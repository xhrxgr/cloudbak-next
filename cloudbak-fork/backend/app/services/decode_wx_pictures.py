# 图片格式的前两个字节固定特征码
import os
import io
import json
import struct
import shutil
import subprocess
import tempfile
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util import Padding

from app.helper.directory_helper import get_wx_dir
from app.models.sys import SysSession
from config.log_config import logger

# 优先使用项目内 ffmpeg，否则回退到系统 PATH 中的 ffmpeg
FFMPEG_BIN = (
    os.environ.get("WX_FFMPEG_BIN")
    or shutil.which("ffmpeg")
    or ""
)

tp = {
    "jpg": 0xFFD8,
    "gif": 0x4749,
    "png": 0x8950,
}

IMAGE_MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG": "png",
    b"GIF": "gif",
    b"wxgf": "wxgf",
}

# 微信 4.x V2 图片加密文件头
V2_HEADER = b"\x07\x08V2\x08\x07"


def _detect_extension(data: bytes) -> str | None:
    """根据解密后数据的文件头判断图片格式"""
    for magic, ext in IMAGE_MAGIC.items():
        if data.startswith(magic):
            return ext
    return None


def _load_image_keys(wx_dir: str):
    """
    读取图片解密密钥。
    优先级：环境变量 > wx_dir/image_keys.json
    """
    aes_key = os.environ.get("WX_IMAGE_AES_KEY")
    xor_key = os.environ.get("WX_IMAGE_XOR_KEY")

    key_file = os.path.join(wx_dir, "image_keys.json")
    if os.path.exists(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not aes_key and cfg.get("aes_key"):
                aes_key = cfg["aes_key"]
            if xor_key is None and cfg.get("xor_key") is not None:
                xor_key = cfg["xor_key"]
        except Exception as e:
            logger.warning("读取 image_keys.json 失败: %s", e)

    if aes_key and isinstance(aes_key, str):
        aes_key = aes_key.strip()
        if aes_key.lower().startswith("0x"):
            aes_key = bytes.fromhex(aes_key[2:])
        elif len(aes_key) in (16, 24, 32):
            # 直接使用 ASCII 字符串作为 AES 密钥
            aes_key = aes_key.encode("utf-8")
        else:
            aes_key = bytes.fromhex(aes_key)

    if xor_key is not None:
        if isinstance(xor_key, str):
            xor_key = int(xor_key, 0)
        else:
            xor_key = int(xor_key)

    return aes_key, xor_key


def _decrypt_v2(data: bytes, aes_key: bytes, xor_key: int) -> bytes | None:
    """微信 4.x V2 图片解密：AES(ECB)+XOR"""
    if len(data) < 0xF:
        return None
    header, body = data[:0xF], data[0xF:]
    _, aes_size, xor_size = struct.unpack("<6sLLx", header)

    # aes_size 是明文长度，实际加密数据会补齐到 AES 块大小的整数倍
    aes_size += AES.block_size - aes_size % AES.block_size

    cipher = AES.new(aes_key, AES.MODE_ECB)
    try:
        aes_data = Padding.unpad(cipher.decrypt(body[:aes_size]), AES.block_size)
    except Exception as e:
        logger.warning("V2 AES 解密失败: %s", e)
        return None

    if xor_size:
        raw_data = body[aes_size:-xor_size]
        xor_data = bytes(b ^ xor_key for b in body[-xor_size:])
    else:
        raw_data = body[aes_size:]
        xor_data = b""

    return aes_data + raw_data + xor_data


def _extract_hevc_nalus(buffer: bytes) -> list[bytes]:
    """从 wxgf 数据中提取 HEVC NALU 裸流。

    wxgf 内部是微信图片的 HEVC 编码，前 4 字节是 "wxgf" 标识，
    之后是若干 NALU（以 00 00 00 01 或 00 00 01 分隔）。
    """
    if len(buffer) < 20 or buffer[:4] != b"wxgf":
        return []

    units: list[bytes] = []
    starts: list[int] = []
    i = 4
    while i < len(buffer) - 3:
        if (buffer[i] == 0 and buffer[i + 1] == 0
                and buffer[i + 2] == 0 and buffer[i + 3] == 1):
            starts.append(i)
            i += 4
            continue
        if (buffer[i] == 0 and buffer[i + 1] == 0
                and buffer[i + 2] == 1):
            starts.append(i)
            i += 3
            continue
        i += 1

    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(buffer)
        prefix_len = 4 if buffer[start + 2] == 0 and buffer[start + 3] == 1 else 3
        payload = buffer[start + prefix_len:end]
        if len(payload) < 2:
            continue
        if (payload[0] & 0x80) == 0:  # 必须有 forbidden_zero_bit=1 才合法
            continue
        units.append(payload)
    return units


def _hevc_to_jpg_via_ffmpeg(hevc_data: bytes) -> bytes | None:
    """将 HEVC 裸流转码为一帧 JPG。需要 ffmpeg 可用。"""
    if not FFMPEG_BIN:
        return None
    with tempfile.TemporaryDirectory(prefix="wxgf_") as tmp:
        tmp_dir = Path(tmp)
        in_path = tmp_dir / "in.hevc"
        out_path = tmp_dir / "out.jpg"
        in_path.write_bytes(hevc_data)
        # 依次尝试不同输入格式
        attempts = [
            ["-f", "hevc", "-i", str(in_path)],
            ["-f", "h265", "-i", str(in_path)],
            ["-i", str(in_path)],
        ]
        for input_args in attempts:
            try:
                if out_path.exists():
                    out_path.unlink()
                result = subprocess.run(
                    [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                     *input_args,
                     "-vframes", "1", "-q:v", "2", "-f", "image2", str(out_path)],
                    timeout=20,
                    capture_output=True,
                )
                if result.returncode == 0 and out_path.exists():
                    jpg = out_path.read_bytes()
                    if jpg and jpg[:3] == b"\xff\xd8\xff":
                        return jpg
            except Exception as e:
                logger.debug("ffmpeg 尝试 %s 失败: %s", input_args, e)
        return None


def _unwrap_wxgf(buffer: bytes) -> bytes:
    """解包 wxgf/WxAM 格式。

    微信 4.x 部分图片使用 HEVC 编码并以 wxgf 头封装。
    解密后如果数据以 'wxgf' 开头但找不到内嵌传统图片签名，
    则按 VPS(32) 分组后用 ffmpeg 转码为 JPG。
    """
    if len(buffer) < 20 or buffer[:4] != b"wxgf":
        return buffer

    # 先尝试找内嵌的传统图片签名
    for i in range(4, min(len(buffer) - 12, 4096)):
        if buffer[i] == 0xFF and buffer[i + 1] == 0xD8 and buffer[i + 2] == 0xFF:
            return buffer[i:]
        if (buffer[i] == 0x89 and buffer[i + 1] == 0x50
                and buffer[i + 2] == 0x4E and buffer[i + 3] == 0x47):
            return buffer[i:]

    units = _extract_hevc_nalus(buffer)
    if not units:
        return buffer

    # 按 VPS (nal_unit_type=32) 分组，找含 VCL 帧的最大组
    def unit_type(u: bytes) -> int:
        return (u[0] >> 1) & 0x3F

    vps_starts = [i for i, u in enumerate(units) if len(u) >= 2 and unit_type(u) == 32]
    candidates: list[tuple[str, bytes]] = []
    for gi, start in enumerate(vps_starts):
        end = vps_starts[gi + 1] if gi + 1 < len(vps_starts) else len(units)
        group = units[start:end]
        if not any(unit_type(u) in (1, 19, 20) for u in group if len(u) >= 2):
            continue
        merged = b"".join(b"\x00\x00\x00\x01" + u for u in group)
        candidates.append((f"vps_group_{gi}", merged))

    candidates.append(("scan_all", b"".join(b"\x00\x00\x00\x01" + u for u in units)))
    candidates.append(("raw_skip4", buffer[4:]))

    for name, hevc in candidates:
        if len(hevc) < 100:
            continue
        jpg = _hevc_to_jpg_via_ffmpeg(hevc)
        if jpg:
            logger.info("wxgf -> jpg via ffmpeg, candidate=%s, size=%d", name, len(jpg))
            return jpg
    return buffer


def decrypt_images(sys_session: SysSession):
    wx_dir = get_wx_dir(sys_session)
    msg_attach_dir = os.path.join(wx_dir, 'FileStorage/MsgAttach/')
    logger.info('图片文件根路径: %s', msg_attach_dir)
    aes_key, xor_key = _load_image_keys(wx_dir)
    decrypt_files_in_directory(msg_attach_dir, aes_key=aes_key, xor_key=xor_key)


def decrypt_files_in_directory(directory, aes_key=None, xor_key=None):
    """
    解密微信目录中的所有图片：jpg,gif,png
    :param directory:
    :return:
    """
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".dat"):
                encrypted_file_path = os.path.join(root, file)
                try:
                    decrypt_file(encrypted_file_path, aes_key=aes_key, xor_key=xor_key)
                except Exception as e:
                    logger.error(f"Failed to decrypt {encrypted_file_path}", e)


def xor_byte_arrays(a, b):
    return bytes([x ^ y for x, y in zip(a, b)])


def match_bytes(a1, a2):
    for key, value in tp.items():
        xor1 = a1 ^ (value >> 8)
        xor2 = a2 ^ (value & 0xFF)
        if xor1 == xor2:
            return xor1, key
    return None


def decrypt_file(encrypted_file_path, aes_key=None, xor_key=None):
    logger.info('decrypt file: %s', encrypted_file_path)

    with open(encrypted_file_path, "rb") as f:
        data = f.read()

    if len(data) < 2:
        logger.warn("File is too small to contain two bytes for matching.")
        return None

    decrypted_data = None
    ext = None

    # 微信 4.x V2 加密
    if data.startswith(V2_HEADER) and aes_key and xor_key is not None:
        decrypted_data = _decrypt_v2(data, aes_key, xor_key)
        if decrypted_data:
            decrypted_data = _unwrap_wxgf(decrypted_data)
            ext = _detect_extension(decrypted_data)
            if ext == "wxgf":
                logger.warning("WxAM 压缩图片无法转码: %s", encrypted_file_path)
                return None
            if not ext:
                logger.warning("V2 解密后无法识别图片格式: %s", encrypted_file_path)
                return None
    else:
        # 旧版纯 XOR 加密
        a1, a2 = data[0], data[1]
        result = match_bytes(a1, a2)
        if result is None:
            logger.warn("No matching key found for the given bytes.")
            return None
        key, ext = result
        decrypted_data = bytes(b ^ key for b in data)

    decrypted_file_path = os.path.splitext(encrypted_file_path)[0] + "." + ext
    with open(decrypted_file_path, "wb") as f:
        f.write(decrypted_data)

    logger.debug('Decrypted file saved as: %s', decrypted_file_path)
    return decrypted_file_path


def decrypt_by_file_type(encrypted_file_path: str, image_type: str, aes_key=None, xor_key=None):
    logger.info('decrypt file: %s', encrypted_file_path)

    tp_bt = tp[image_type]
    if not tp_bt:
        return None

    with open(encrypted_file_path, "rb") as f:
        first_byte = f.read(1)
        second_byte = f.read(1)

        if len(first_byte) < 1 or len(second_byte) < 1:
            logger.warn("File is too small to contain two bytes for matching.")
            return None

        a1 = first_byte[0]

    # 使用前两个字节进行匹配
    key = a1 ^ (tp_bt >> 8)

    # 生成解密后的文件路径
    decrypted_file_path = os.path.splitext(encrypted_file_path)[0] + "." + image_type

    # 使用 key 对整个文件进行异或运算并写入新的文件
    with open(encrypted_file_path, "rb") as encrypted_file:
        with open(decrypted_file_path, "wb") as decrypted_file:
            while byte := encrypted_file.read(1):
                decrypted_file.write(bytes([byte[0] ^ key]))

    logger.debug('Decrypted file saved as: %s', decrypted_file_path)
    return decrypted_file_path


def decrypt_file_return_io(encrypted_file_path, aes_key=None, xor_key=None):
    """
    解密返回字节流，供接口直接返回
    """
    logger.info('decrypt file: %s', encrypted_file_path)

    with open(encrypted_file_path, "rb") as f:
        data = f.read()

    if len(data) < 2:
        logger.warn("File is too small.")
        return None

    decrypted_data = None

    if data.startswith(V2_HEADER) and aes_key and xor_key is not None:
        decrypted_data = _decrypt_v2(data, aes_key, xor_key)
        if decrypted_data:
            decrypted_data = _unwrap_wxgf(decrypted_data)
    else:
        a1, a2 = data[0], data[1]
        result = match_bytes(a1, a2)
        if result is None:
            logger.warn("No matching key found for the given bytes.")
            return None
        key, _ = result
        decrypted_data = bytes(b ^ key for b in data)

    if decrypted_data is None:
        return None

    decrypted_stream = io.BytesIO(decrypted_data)
    decrypted_stream.seek(0)
    logger.debug('Decrypted file data prepared.')
    return decrypted_stream
