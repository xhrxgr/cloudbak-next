import array
import hashlib
from datetime import datetime
from typing import Optional

from google.protobuf.internal import decoder
from sqlalchemy import inspect, select, func, literal_column

from app.enum.msg_enum import FilterMode
from config.log_config import logger
from wx.common.filters.msg_filter import MsgFilterObj, SingleMsgFilterObj
from wx.common.output.message import MsgSearchOut, Msg, WindowsV4Properties
from wx.interface.wx_interface import MessageManager, ClientInterface
from wx.win.v4.enums.v4_enums import V4DBEnum
from wx.win.v4.models.message_model import DynamicModel, Name2Id
import zstandard as zstd

from wx.win.v4.utils.zstandard_utils import ZstandardUtils


def _extract_packed_md5(packed_info_data: bytes) -> Optional[str]:
    """从 packed_info_data 的 protobuf 字段 3 中提取图片文件 md5"""
    if not packed_info_data:
        return None
    try:
        pos = 0
        md5 = None
        while pos < len(packed_info_data):
            tag, pos = decoder._DecodeVarint(packed_info_data, pos)
            wire_type = tag & 7
            field_number = tag >> 3
            if wire_type == 2:
                length, pos2 = decoder._DecodeVarint(packed_info_data, pos)
                value = packed_info_data[pos2:pos2 + length]
                pos = pos2 + length
                if field_number == 3:
                    md5 = value.decode("utf-8", errors="ignore").strip().strip('"').strip()
            elif wire_type == 0:
                _, pos = decoder._DecodeVarint(packed_info_data, pos)
            elif wire_type == 5:
                pos += 4
            elif wire_type == 1:
                pos += 8
            else:
                break
        return md5
    except Exception as e:
        logger.warning("解析 packed_info_data 失败: %s", e)
        return None


def _build_image_paths(username: str, create_time: int, md5: str) -> tuple[str, str]:
    """构造微信 4.x 图片缩略图与原图的相对路径"""
    user_hash = DynamicModel.md5_username(username)
    month = datetime.fromtimestamp(create_time).strftime("%Y-%m")
    thumb = f"msg/attach/{user_hash}/{month}/Img/{md5}_t.dat"
    source = f"msg/attach/{user_hash}/{month}/Img/{md5}_h.dat"
    return thumb, source


class MessageManagerWindowsV4(MessageManager):

    def __init__(self, client: ClientInterface):
        self.client = client
        self.dynamic = DynamicModel()
        # username 映射的 db 库
        self.user_db_mapping = {}
        self.message_db_name_array = []

    def clear(self):
        self.user_db_mapping.clear()
        self.message_db_name_array.clear()

    def get_message_db_name_array(self):
        """
        获取并缓存message库文件列表
        """
        if not self.message_db_name_array:
            self.message_db_name_array = self.client.get_db_manager().messages_db_name_array()
        return self.message_db_name_array

    def get_message_engine_by_db_name(self, db_name: str):
        db_path = f"{V4DBEnum.MESSAGE_DB_FOLDER}/{db_name}"
        return self.client.get_db_manager().wx_db_engine(db_path)

    def get_message_session_maker_by_db_name(self, db_name: str):
        db_path = f"{V4DBEnum.MESSAGE_DB_FOLDER}/{db_name}"
        return self.client.get_db_manager().wx_db(db_path)

    def get_table_name_db_list(self, username: str, table_name: str):
        """
        获取table_name存在的db列表
        """
        array = self.get_message_db_name_array()
        # 存在缓存，直接返回
        if table_name in self.user_db_mapping:
            return self.user_db_mapping[table_name]
        # 不存在缓存，遍历 message_\d.db，检查是否存在表名
        user_db_names = []
        for filename in array:
            engine = self.get_message_engine_by_db_name(filename)
            inspector = inspect(engine)
            if inspector.has_table(table_name):
                logger.info(f"{filename} 中存在 {table_name}")
                user_db_names.append(filename)
        # 按 create_time 排序
        user_db_names = self.sort_db_by_table_name(username, user_db_names)
        # 缓存 username -> [message_\d.db]
        self.user_db_mapping[table_name] = user_db_names
        return user_db_names

    def sort_db_by_table_name(self, username: str, db_array):
        """
        将 db 按照 table_name create_time 时间倒排序
        """
        logger.info(f"原始排序 {db_array}")
        message_model = DynamicModel.get_dynamic_message_model(username)
        create_time_array = []
        for db_name in db_array:
            logger.info(f"库名：{db_name}")
            sm = self.get_message_session_maker_by_db_name(db_name)
            with sm() as db:
                msg = db.query(message_model).order_by(message_model.create_time.desc()).first()
                if msg:  # 只有查询到消息时才加入排序
                    create_time_array.append({
                        "db_name": db_name,
                        "create_time": msg.create_time
                    })
        create_time_array.sort(key=lambda x: x["create_time"], reverse=True)
        final_array = []
        for o in create_time_array:
            final_array.append(o["db_name"])

        logger.info(f"最终排序 {final_array}")
        return final_array

    def messages_filter_page(self, filter_obj: MsgFilterObj) -> MsgSearchOut:
        # 获取动态表
        message_model = DynamicModel.get_dynamic_message_model(filter_obj.username)
        # 获取动态表名
        table_name = message_model.__tablename__
        logger.info(table_name)
        db_name_array = self.get_table_name_db_list(filter_obj.username, table_name)
        # 跨库分页查询
        left = filter_obj.size  # 剩余查询数量
        offset = filter_obj.start if filter_obj.start else 0  # 查询偏移量，初始为客户端送的值
        is_start = False  # 用于判断查询开始
        current_db = None
        msgs = []  # 返回的消息列表
        db_start = False
        for db_name in db_name_array:
            if not is_start:
                if filter_obj.start_db is None or filter_obj.start_db == '':
                    db_start = True
                elif filter_obj.start_db == db_name:
                    db_start = True
            if not db_start:
                logger.info(f"跳过库：{db_name}")
                continue
            current_db = db_name
            logger.info(f"查询库 {db_name}")
            limit = left
            name2id_subquery = (
                select(
                    literal_column("Name2Id.rowid").label("row_num"),
                    Name2Id.user_name
                ).subquery("b")
            )
            stmt = (
                select(message_model, name2id_subquery)
                .join(name2id_subquery, message_model.real_sender_id == name2id_subquery.c.row_num, isouter=True)
                .offset(offset)
                .limit(limit)
            )
            # 根据查询模式确定排序方向
            if filter_obj.filter_mode == FilterMode.DESC:
                stmt = stmt.order_by(message_model.sort_seq.desc())
            else:
                stmt = stmt.order_by(message_model.sort_seq.asc())

            sm = self.get_message_session_maker_by_db_name(db_name)
            with sm() as db:
                results = db.execute(stmt).all()
                for row in results:
                    m = row[0]
                    n = row[2] if len(row) == 3 else None
                    msg = WindowsV4Properties(**m.__dict__)
                    msg.sender = n
                    msg.message_content_data = ZstandardUtils.convert_zstandard(m.message_content)
                    msg.source_data = ZstandardUtils.convert_zstandard(m.source)
                    msg.compress_content_data = ZstandardUtils.convert_zstandard(m.compress_content)
                    # 图片消息：从 packed_info_data 提取 md5 并构造相对路径
                    if msg.local_type == 3 and m.packed_info_data:
                        md5 = _extract_packed_md5(m.packed_info_data)
                        if md5:
                            try:
                                msg.thumb, msg.source = _build_image_paths(
                                    filter_obj.username, msg.create_time, md5
                                )
                            except Exception as e:
                                logger.warning("构造图片路径失败: %s", e)
                    msgs.append(Msg(windows_v4_properties=msg))

                # 判断查询结果数量
                data_count = len(results)
                logger.info(f"预期 {limit}, 实际 {data_count}")
                left = left - data_count
                offset = offset + limit
                logger.info(f"剩余 {left}")
                if left <= 0:
                    logger.info("查询结束")
                    break
                else:
                    offset = 0
            logger.info(db_name)
        return MsgSearchOut(start=offset, start_db=current_db, messages=msgs)

    def message(self, filter_obj: SingleMsgFilterObj) -> Msg | None:
        # 获取动态表
        message_model = DynamicModel.get_dynamic_message_model(filter_obj.username)
        # 获取动态表名
        table_name = message_model.__tablename__
        logger.info(table_name)

        sm = self.get_message_session_maker_by_db_name(filter_obj.db_name)
        with sm() as db:
            msg = db.query(message_model).filter(message_model.local_id.is_(filter_obj.local_id)).one()
            logger.info(f"msg is : {msg}")
        with open('d:/packed_info_data', 'wb') as f:
            f.write(msg.packed_info_data)
        return None
