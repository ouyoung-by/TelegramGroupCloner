import configparser
import logging
import os
import asyncio
from typing import Dict
from collections import defaultdict

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest, DeletePhotosRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import MessageMediaDocument, InputPhoto, InputChannel

sessions_dir = 'sessions'
clients_pool = {}
client_locks = {}
sender_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
message_id_mapping = {}
cloned_users = set()
blacklist = set()
replacements = {}
API_ID = 0
API_HASH = ''
SOURCE_GROUP: InputChannel
TARGET_GROUP: InputChannel
PROXY_HOST = None
PROXY_PORT = None
PROXY_TYPE = None
proxy = None

logging.getLogger('telethon').setLevel(logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


async def login_new_account():
    global proxy
    phone = input("输入手机号: ")
    if PROXY_TYPE and PROXY_HOST and PROXY_PORT:
        proxy = (PROXY_TYPE, PROXY_HOST, PROXY_PORT)
    client = TelegramClient(f"{sessions_dir}/{phone}", API_ID, API_HASH, proxy=proxy)
    await client.connect()

    if not await client.is_user_authorized():
        y = await client.send_code_request(phone)
        code = input('输入验证码: ')
        try:
            await client.sign_in(phone, code, phone_code_hash=y.phone_code_hash)
        except SessionPasswordNeededError:
            password = input("请输入2FA 密码: ")
            await client.sign_in(password=password)

    logger.info(f"克隆账号登录成功: {phone}")
    await check_and_join_target(client)


async def load_existing_sessions():
    global proxy
    for filename in os.listdir(sessions_dir):
        if filename.endswith('.session'):
            session_name = filename.replace('.session', '')
            if PROXY_TYPE and PROXY_HOST and PROXY_PORT:
                proxy = (PROXY_TYPE, PROXY_HOST, PROXY_PORT)

            client = TelegramClient(f"{sessions_dir}/{session_name}", API_ID, API_HASH, proxy=proxy)
            await client.start()
            logger.info(f"加载 session 成功: {session_name}")
            await check_and_join_target(client)
            await delete_profile_photos(client)
            clients_pool[client] = None
            client_locks[client] = asyncio.Lock()


async def delete_profile_photos(client: TelegramClient) -> None:
    try:
        me = await client.get_me()
        photos = await client.get_profile_photos(me.id)
        for photo in photos:
            await client(DeletePhotosRequest([
                InputPhoto(
                    id=photo.id,
                    access_hash=photo.access_hash,
                    file_reference=photo.file_reference
                )]))
        logger.info(f"[{me.phone}] 清空历史头像成功")
    except Exception as e:
        logger.error(e)


async def check_and_join_target(client: TelegramClient) -> None:
    try:
        await client(JoinChannelRequest(TARGET_GROUP))
        me = await client.get_me()
        logger.info(f"[{me.phone}] 加入目标群组成功")
    except Exception as e:
        if "FROZEN_METHOD_INVALID" in str(e):
            await cleanup_frozen_client(client)
            logger.error(f"克隆账号加入目标群组失败: {e}")
        else:
            logger.info(e)


async def check_and_join_source(client: TelegramClient):
    try:
        await client(JoinChannelRequest(SOURCE_GROUP))
        logger.info("监听账号加入源群组成功")
    except Exception as e:
        if "FROZEN_METHOD_INVALID" in str(e):
            await cleanup_frozen_client(client)
            logger.error(f"监听账号加入源群组失败: {e}")


async def clone_and_forward_message(event, monitor_client: TelegramClient):
    sender = await event.get_sender()

    if sender.bot:
        logger.info("非普通用户消息，跳过处理")
        return

    sender_id = sender.id
    lock = sender_locks[sender_id]
    async with lock:
        if sender_id in blacklist:
            logger.info(f"用户 {sender_id} 在黑名单中，跳过克隆")
            return

        # 已分配过的 client
        for client, cloned_user in clients_pool.items():
            if cloned_user == sender_id:
                lock = client_locks[client]
                async with lock:
                    try:
                        await forward_message_as(
                            client, event, monitor_client)
                        logger.info(f"已转发 {sender_id} 的新消息")
                    except Exception as e:
                        if "FROZEN_METHOD_INVALID" in str(e):
                            await cleanup_frozen_client(client, sender_id)
                        logger.info(f"转发失败（已克隆用户）: {e}")
                return

        # 未分配的 client
        for client, cloned_user in clients_pool.items():
            client: TelegramClient
            if cloned_user is None:
                lock = client_locks[client]
                async with lock:  # <== 关键！锁住整个设置流程
                    try:
                        # 再次检查是否被其他协程分配了
                        if clients_pool[client] is not None:
                            continue

                        # 设置昵称
                        await client(UpdateProfileRequest(
                            first_name=sender.first_name or " ",
                            last_name=sender.last_name or "",
                        ))
                        me = await client.get_me()
                        logger.info(f"[{me.phone}] 设置昵称成功")

                        # 设置头像
                        try:
                            photos = await monitor_client.get_profile_photos(sender, limit=1)
                            if photos:
                                profile_path = await monitor_client.download_media(photos[0])
                                if profile_path and os.path.exists(profile_path):
                                    uploaded = await client.upload_file(file=profile_path)
                                    if photos[0].video_sizes:
                                        await client(UploadProfilePhotoRequest(video=uploaded))
                                    else:
                                        await client(UploadProfilePhotoRequest(file=uploaded))
                                    os.remove(profile_path)
                                    logger.info(f"[{me.phone}] 设置头像成功")
                                else:
                                    logger.warning("头像无法下载")
                            else:
                                logger.info("目标用户没有头像")
                        except Exception as e:
                            logger.error(f"设置头像失败: {e}")

                        # 发送消息
                        await forward_message_as(
                            client, event, monitor_client)

                        # 分配完成
                        clients_pool[client] = sender_id
                        cloned_users.add(sender_id)
                        logger.info(f"[{me.phone}] 完成新用户克隆: {sender_id}")
                    except Exception as e:
                        if "FROZEN_METHOD_INVALID" in str(e):
                            await cleanup_frozen_client(client, sender_id)
                        logger.warning(f"克隆失败: {e}")
                    return

        logger.info("无可用账号进行克隆")


def is_animated_sticker_or_video(message):
    if isinstance(message.media, MessageMediaDocument) and message.media.document:
        mime_type = message.media.document.mime_type
        return mime_type.startswith("application/x-tgsticker") or mime_type.startswith("video/webm")
    return False


async def forward_message_as(client, event, monitor_client):
    message = event.message
    text = apply_replacements(message.text or "")

    try:
        if message.is_reply:
            try:
                reply = await event.get_reply_message()
                if not reply:
                    logger.warning("无法获取被回复消息")
                    return

                logger.info(f"找到被回复消息: {reply.id}, 来自: {reply.sender_id}")

                # 映射查找
                if reply.id in message_id_mapping:
                    reply_to_msg_id = message_id_mapping[reply.id]
                else:
                    logger.info("没有找到对应的克隆账号消息，跳过回复")
                    return

                # 发送消息（回复）
                if is_animated_sticker_or_video(message):
                    file_path = await monitor_client.download_media(message)
                    original_attributes = message.media.document.attributes

                    # 发送文件，保持原有属性
                    sent_reply = await client.send_file(
                        TARGET_GROUP,
                        file_path,
                        force_document=False,
                        supports_streaming=True,
                        attributes=original_attributes,
                        reply_to=reply_to_msg_id
                    )

                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)

                elif message.media:
                    file_path = await monitor_client.download_media(message)
                    sent_reply = await client.send_file(
                        TARGET_GROUP,
                        file_path,
                        caption=text,
                        reply_to=reply_to_msg_id
                    )
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                else:
                    sent_reply = await client.send_message(
                        TARGET_GROUP,
                        text,
                        reply_to=reply_to_msg_id
                    )

                message_id_mapping[message.id] = sent_reply.id

            except Exception as e:
                logger.warning(f"获取被回复消息失败: {e}")
        else:
            try:
                if is_animated_sticker_or_video(message):
                    file_path = await monitor_client.download_media(message)
                    original_attributes = message.media.document.attributes

                    # 发送文件，保持原有属性
                    sent = await client.send_file(
                        TARGET_GROUP,
                        file_path,
                        force_document=False,
                        supports_streaming=True,
                        attributes=original_attributes,
                    )

                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)

                elif message.media:
                    file_path = await monitor_client.download_media(message)
                    sent = await client.send_file(
                        TARGET_GROUP,
                        file_path,
                        caption=text
                    )
                    if file_path and os.path.exists(file_path):
                        os.remove(file_path)
                else:
                    sent = await client.send_message(
                        TARGET_GROUP,
                        text
                    )

                message_id_mapping[message.id] = sent.id

            except Exception as e:
                logger.error(f"[!] 发送当前消息失败: {e}")

    except Exception as e:
        logger.error(f"获取当前用户信息失败: {e}")


def apply_replacements(text: str) -> str:
    if not text:
        return text
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


async def cleanup_frozen_client(client: TelegramClient, sender_id: int = None):
    try:
        session_file = client.session.filename
        logger.info(f"检测到被冻结账号 {session_file}")

        # 断开连接
        await client.disconnect()

        # 从管理结构中移除
        clients_pool.pop(client, None)
        client_locks.pop(client, None)

        if sender_id:
            cloned_users.discard(sender_id)

    except Exception as e:
        logger.warning(f"清理被冻结账号失败: {e}")


def load_config():
    config_path = "setting/config.ini"
    default_content = """[telegram]
api_id = 9597683
api_hash = 9981e2f10aeada4452a9538921132099
source_group = ouyoung
target_group = ouyoung

[proxy]
host = 127.0.0.1
port = 7890
type = socks5

[blacklist]
user_ids = 123456789,987654321

[replacements]
a = b
你好 = 我好
"""

    os.makedirs("setting", exist_ok=True)
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(default_content)
            logger.info(f"已初始化配置文件: {config_path}")

    config = configparser.ConfigParser()
    try:
        config.read(config_path, encoding="utf-8")

        # 读取 API 配置
        global API_ID, API_HASH, SOURCE_GROUP, TARGET_GROUP, PROXY_HOST, PROXY_PORT, PROXY_TYPE
        API_ID = int(config.get("telegram", "api_id"))
        API_HASH = config.get("telegram", "api_hash")
        SOURCE_GROUP = config.get("telegram", "source_group")
        TARGET_GROUP = config.get("telegram", "target_group")

        # 读取代理配置
        PROXY_HOST = config.get("proxy", "host")
        PROXY_PORT = int(config.get("proxy", "port"))
        PROXY_TYPE = config.get("proxy", "type")

        # 更新黑名单
        blacklist_str = config.get("blacklist", "user_ids", fallback="")
        blacklist.clear()
        blacklist.update(int(uid.strip()) for uid in blacklist_str.split(",") if uid.strip().isdigit())

        # 更新替换词
        replacements.clear()
        if config.has_section("replacements"):
            replacements.update(dict(config.items("replacements")))

        logger.info(f"成功加载配置文件: {config_path}")
    except Exception as e:
        logger.error(f"配置加载失败: {e}")


async def start_monitor():
    global proxy
    session_file = 'monitor_session.session'
    if PROXY_TYPE and PROXY_HOST and PROXY_PORT:
        proxy = (PROXY_TYPE, PROXY_HOST, PROXY_PORT)
    monitor_client = TelegramClient("monitor_session", API_ID, API_HASH, proxy=proxy)

    await monitor_client.connect()

    if not await monitor_client.is_user_authorized():
        if not os.path.exists(session_file):
            phone = input('请输入监听账号手机号: ')
            y = await monitor_client.send_code_request(phone)
            code = input('输入验证码: ')
            try:
                await monitor_client.sign_in(phone, code, phone_code_hash=y.phone_code_hash)
            except SessionPasswordNeededError:
                password = input("请输入2FA 密码: ")
                await monitor_client.sign_in(password=password)
        else:
            logger.info("找到 session 文件但未授权，可能被清除或失效。请重新登录。")
            phone = input('请输入监听账号手机号: ')
            y = await monitor_client.send_code_request(phone)
            code = input('输入验证码: ')
            try:
                await monitor_client.sign_in(phone, code, phone_code_hash=y.phone_code_hash)
            except SessionPasswordNeededError:
                password = input("请输入2FA 密码: ")
                await monitor_client.sign_in(password=password)
    me = await monitor_client.get_me()
    logger.info(f"监听账号登录成功: {me.phone}")

    # 检查是否已经加入源群组
    try:
        await check_and_join_source(monitor_client)
    except Exception as e:
        if "FROZEN_METHOD_INVALID" in str(e):
            await cleanup_frozen_client(monitor_client)
        logger.error(f"监听账号加入源群组失败: {str(e)}")

    # 监听群组消息
    @monitor_client.on(events.NewMessage(chats=SOURCE_GROUP))
    async def handler(event):
        await clone_and_forward_message(event, monitor_client)

    await monitor_client.run_until_disconnected()


async def main():
    os.system("title TelegramGroupCloner v1.1.0")

    load_config()

    print("\033[33m免责声明: 本程序仅供学习、研究和技术探讨用途，"
          "开发者不对因使用本程序而导致的任何直接或间接损失承担责任，"
          "任何通过该程序进行的违法活动，责任应由用户自行承担，开发者对此概不负责，"
          "请遵循当地法律法规，在合法的范围内使用本程序，"
          "如有疑问，请务必遵循相应平台的使用条款与政策。\033[0m")
    print("\033[31m开发者: 欧阳\033[0m")
    print("\033[31mTG: https://t.me/ouyoung\033[0m")
    print("\033[31mTG交流群: https://t.me/oyDevelopersClub\033[0m")

    os.makedirs(sessions_dir, exist_ok=True)
    print("\n↓↓↓↓↓↓↓ 选择你要执行的操作 ↓↓↓↓↓↓↓")

    while True:
        print("\n1. 新增账号")
        print("2. 使用现有账号")
        print("0. 退出程序")

        try:
            choice = input("请选择操作: ").strip()
        except KeyboardInterrupt:
            print("\n已取消，退出程序")
            break

        if choice == '1':
            await login_new_account()
        elif choice == '2':
            await load_existing_sessions()
            await start_monitor()
        elif choice == '0':
            logger.info("退出程序成功")
            break
        else:
            logger.info("无效选择，请重新输入")


if __name__ == '__main__':
    asyncio.run(main())
