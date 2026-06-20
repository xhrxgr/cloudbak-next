"""微信 4.x 消息内容解析工具。

提供按 local_type 解析 message_content_data 字符串的能力。
支持文本、图片、视频、语音、文件、位置、名片、链接、小程序、音乐、表情、转账、红包、合并转发等。

输入：message_content_data（可能为 XML、JSON 字符串、二进制 protobuf 等）。
输出：结构化 dict，便于前端直接渲染。
"""
from __future__ import annotations

import base64
import json
import re
import struct
from typing import Any, Optional
from xml.etree import ElementTree as ET

from config.log_config import logger


# 微信 local_type 数值常量
TYPE_TEXT = 1
TYPE_IMAGE = 3
TYPE_VOICE = 34
TYPE_VIDEO = 43
TYPE_EMOJI = 47
TYPE_LOCATION = 48
TYPE_APP_MSG = 49            # 卡片/链接/小程序/音乐/合并转发 等应用消息
TYPE_VOIP_INVITE = 50
TYPE_SYS = 10000
TYPE_PAT = 266287972401
TYPE_REVOKE = 266287972401  # 撤回消息 local_type


def _strip_text_payload(data: Optional[str]) -> Optional[str]:
    """提取纯文本部分（形如 wxid_xxx:\\n 内容）。"""
    if not data:
        return None
    s = data
    if ":\n" in s:
        s = s.split(":\n", 1)[1]
    return s.strip()


def _parse_xml_safe(xml_str: Optional[str]) -> Optional[ET.Element]:
    if not xml_str:
        return None
    try:
        # 微信 XML 多以 <?xml ... ?> 开头，ET 可直接吃
        return ET.fromstring(xml_str)
    except Exception as e:
        logger.debug("XML 解析失败: %s", e)
        return None


def _xml_attrs(root: Optional[ET.Element]) -> dict:
    if root is None:
        return {}
    out = dict(root.attrib or {})
    return out


def _xml_first_attr(root: Optional[ET.Element], name: str) -> Optional[str]:
    return _xml_attrs(root).get(name)


def _parse_voice(buf: Optional[str]) -> dict:
    """语音消息：直接是 silk/silkv3 字节流 base64 表示 + 时长信息。"""
    if not buf:
        return {}
    # 微信语音 base64 字符串长度
    size = len(buf)
    # 时长（秒）需 silk 解码才知，这里用 base64 解码长度粗估
    try:
        raw_len = len(base64.b64decode(buf))
    except Exception:
        raw_len = size
    return {
        "silk_base64": buf,
        "approx_size": raw_len,
        "duration_hint": max(1, raw_len // 4000),  # 粗略估算
    }


def _parse_video_xml(xml_str: Optional[str]) -> dict:
    """视频消息：XML 形式，包含 videomsg、fromusername、md5 等。"""
    root = _parse_xml_safe(xml_str)
    if root is None:
        return {}
    videomsg = root.find("videomsg")
    attrs = _xml_attrs(videomsg) if videomsg is not None else _xml_attrs(root)
    return {
        "raw_xml": xml_str,
        "md5": attrs.get("md5") or attrs.get("newmd5"),
        "length": attrs.get("length"),
        "play_length": attrs.get("playlength") or attrs.get("play_length"),
        "from_username": _xml_first_attr(root, "fromusername"),
        "cdn_thumb_url": attrs.get("cdnbigimgurl") or attrs.get("cdnthumburl"),
    }


def _parse_emoji_xml(xml_str: Optional[str]) -> dict:
    root = _parse_xml_safe(xml_str)
    if root is None:
        return {}
    emoji = root.find("emoji")
    attrs = _xml_attrs(emoji) if emoji is not None else _xml_attrs(root)
    return {
        "md5": attrs.get("md5") or attrs.get("encrypturl"),
        "width": attrs.get("width"),
        "height": attrs.get("height"),
        "cdn_url": attrs.get("cdnurl"),
    }


def _parse_location_xml(xml_str: Optional[str]) -> dict:
    root = _parse_xml_safe(xml_str)
    if root is None:
        return {}
    loc = root.find("location")
    attrs = _xml_attrs(loc) if loc is not None else _xml_attrs(root)
    return {
        "label": attrs.get("label"),
        "poiname": attrs.get("poiname"),
        "lng": attrs.get("lng"),
        "lat": attrs.get("lat"),
        "scale": attrs.get("scale"),
    }


def _parse_app_msg_xml(xml_str: Optional[str]) -> dict:
    """type=49 的应用消息。子类型由 appmsg.type 决定。

    常见 type 值：
    - 5 / 33：链接/卡片
    - 6：文件
    - 8：图片（带描述）
    - 19：合并转发聊天记录
    - 21：位置
    - 33 / 36：小程序
    - 2000：转账
    - 2001：红包封面
    """
    root = _parse_xml_safe(xml_str)
    if root is None:
        return {}
    appmsg = root.find("appmsg")
    app_attrs = _xml_attrs(appmsg) if appmsg is not None else _xml_attrs(root)
    sub_type = app_attrs.get("type") or app_attrs.get("appmsg") or ""

    info: dict[str, Any] = {
        "app_type": sub_type,
        "title": app_attrs.get("title"),
        "desc": app_attrs.get("des"),
        "url": app_attrs.get("url"),
        "from_username": _xml_first_attr(root, "fromusername"),
    }

    # 链接卡片
    if sub_type in ("5", "33", "36", "63", "75"):
        # 子节点信息
        for child_name in ("carditem", "url", "weappinfo", "appinfo"):
            child = appmsg.find(child_name) if appmsg is not None else None
            if child is not None:
                info[child_name] = _xml_attrs(child)

    # 文件
    if sub_type == "6":
        fileinfo = appmsg.find("appattach") if appmsg is not None else None
        if fileinfo is not None:
            fa = _xml_attrs(fileinfo)
            info.update({
                "file_name": fa.get("filename"),
                "file_ext": fa.get("fileext"),
                "file_size": fa.get("totallen"),
                "file_aes_key": fa.get("aeskey"),
                "file_cdn_url": fa.get("cdnattachurl"),
            })

    # 合并转发
    if sub_type == "19":
        record = appmsg.find("recordinfo") if appmsg is not None else None
        if record is not None:
            ra = _xml_attrs(record)
            info.update({
                "record_xml": ET.tostring(record, encoding="unicode"),
                "title": ra.get("title") or info.get("title"),
                "desc": ra.get("desc") or info.get("desc"),
            })
        # 包含内嵌的 recorditem* 摘要
        record_items = []
        if appmsg is not None:
            for ri in appmsg.findall("recorditem"):
                record_items.append({
                    "type": _xml_first_attr(ri, "type"),
                    "title": _xml_first_attr(ri, "title"),
                    "desc": _xml_first_attr(ri, "desc"),
                    "data_source": _xml_first_attr(ri, "datasourceid"),
                })
        if record_items:
            info["record_items"] = record_items[:10]  # 最多展示 10 条摘要

    # 转账
    if sub_type == "2000":
        info.update({
            "amount": app_attrs.get("des") or app_attrs.get("title"),
            "pay_subtype": app_attrs.get("subtype"),
        })

    # 红包 / 红包封面
    if sub_type in ("2001", "2002"):
        info.update({
            "red_envelope": True,
            "title": app_attrs.get("title"),
        })

    return info


def _parse_pat_text(text: Optional[str]) -> dict:
    """拍一拍 / 系统提示。文本中常包含 '拍了拍' / '戳了戳'。"""
    if not text:
        return {}
    return {"raw": text}


def _parse_revoke_text(text: Optional[str]) -> dict:
    if not text:
        return {}
    return {"raw": text}


def _parse_link_url(text: Optional[str]) -> dict:
    """文本中的 URL 链接抽取。"""
    if not text:
        return {"urls": []}
    urls = re.findall(r"https?://[^\s\"'<>]+", text)
    return {"urls": urls}


def parse_message(local_type: int, message_content: Optional[str]) -> dict:
    """根据 local_type 解析消息内容，返回结构化字典。"""
    if not message_content:
        return {"kind": "empty"}

    text = _strip_text_payload(message_content) or message_content

    # 应用消息
    if local_type == TYPE_APP_MSG:
        info = _parse_app_msg_xml(text)
        info["kind"] = "app"
        return info

    if local_type == TYPE_VOICE:
        info = _parse_voice(text if text != message_content else message_content)
        info["kind"] = "voice"
        return info

    if local_type == TYPE_VIDEO:
        info = _parse_video_xml(text)
        info["kind"] = "video"
        return info

    if local_type == TYPE_EMOJI:
        info = _parse_emoji_xml(text)
        info["kind"] = "emoji"
        return info

    if local_type == TYPE_LOCATION:
        info = _parse_location_xml(text)
        info["kind"] = "location"
        return info

    if local_type == TYPE_TEXT:
        info = _parse_link_url(text)
        info["kind"] = "text"
        info["content"] = text
        return info

    if local_type in (TYPE_PAT,):
        info = _parse_pat_text(text)
        info["kind"] = "pat"
        return info

    if local_type == TYPE_REVOKE:
        info = _parse_revoke_text(text)
        info["kind"] = "revoke"
        return info

    if local_type == TYPE_SYS:
        info = _parse_revoke_text(text)
        info["kind"] = "system"
        return info

    if local_type == TYPE_VOIP_INVITE:
        return {"kind": "voip_invite", "raw": text}

    if local_type == TYPE_IMAGE:
        # 纯图片时，message_content_data 可能是 protobuf packed_info_data。
        # 图片路径解析由 v4_message_data 在 packed_info_data 处理。
        return {"kind": "image"}

    # 其它未知类型
    return {"kind": "unknown", "raw": message_content[:512]}
