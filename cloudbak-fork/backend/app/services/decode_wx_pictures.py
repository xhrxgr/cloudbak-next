# 图片格式的前两个字节固定特征码
import os
import io
import json
import struct

from Crypto.Cipher import AES
from Crypto.Util import Padding

from app.helper.directory_helper import get_wx_dir
from app.models.sys import SysSession
from config.log_config import logger

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
            ext = _detect_extension(decrypted_data)
            if ext == "wxgf":
                logger.warning("WxAM 压缩图片暂不支持自动解压: %s", encrypted_file_path)
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
