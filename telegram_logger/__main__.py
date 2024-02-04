import asyncio
import logging
import os
import pickle
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Union

from telethon import TelegramClient, events
from telethon.events import MessageDeleted, MessageEdited, NewMessage
from telethon.hints import Entity
from telethon.tl.custom import Message
from telethon.tl.functions.messages import SaveGifRequest, SaveRecentStickerRequest
from telethon.tl.types import (
    Channel,
    Chat,
    Contact,
    Document,
    DocumentAttributeAnimated,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    InputDocument,
    MessageMediaContact,
    MessageMediaDice,
    MessageMediaGame,
    MessageMediaGeo,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaWebPage,
    PeerChannel,
    PeerChat,
    PeerUser,
    Photo,
    TypeMessageMedia,
    UpdateReadMessagesContents,
)

from telegram_logger import encryption
from telegram_logger.database import DbMessage, register_models
from telegram_logger.database.methods import (
    delete_expired_messages_from_db,
    get_message_ids_by_event,
    message_exists,
    save_message,
)
from telegram_logger.settings import settings
from telegram_logger.types import ChatType

client: TelegramClient
MY_ID: int


async def get_chat_type(event: NewMessage.Event) -> ChatType:
    # chats and supergroups
    if event.is_group:
        return ChatType.GROUP
    # supergroups and channels
    if event.is_channel:
        return ChatType.CHANNEL
    # direct messages
    if event.is_private:
        return ChatType.BOT if (await event.get_sender()).bot else ChatType.USER
    return ChatType.UNKNOWN


async def new_message_handler(event: Union[NewMessage.Event, MessageEdited.Event]):
    chat_id = event.chat_id
    from_id = get_sender_id(event.message)
    msg_id = event.message.id

    if (
        chat_id == settings.log_chat_id
        and from_id == MY_ID
        and event.message.text
        and (
            re.match(r"^(https://)?t\.me/(?:c/)?\w+/\d+", event.message.text)
            or re.match(r"^tg://openmessage\?user_id=\d+&message_id=\d+", event.message.text)
        )
    ):
        msg_links = re.findall(r"(?:https://)?t\.me/(?:c/)?\w+/\d+", event.message.text)
        if not msg_links:
            msg_links = re.findall(
                r"tg://openmessage\?user_id=\d+&message_id=\d+", event.message.text
            )
        if msg_links:
            for msg_link in msg_links:
                await save_restricted_msg(msg_link)
            return

    if from_id in settings.ignored_ids or chat_id in settings.ignored_ids:
        return

    edited_at = None
    noforwards = False
    self_destructing = False

    try:
        noforwards = event.chat.noforwards is True
    except AttributeError:  # AttributeError: 'User' object has no attribute 'noforwards'
        noforwards = event.message.noforwards is True

    # noforwards = False  # wtf why does it work now?

    try:
        if event.message.media.ttl_seconds:
            self_destructing = True
    except AttributeError:
        pass

    if event.message.media and (noforwards or self_destructing):
        await save_media_as_file(event.message)

    if isinstance(event, MessageEdited.Event):
        edited_at = datetime.now(timezone.utc)  # event.message.edit_date

    if not await message_exists(msg_id):
        media = pickle.dumps(event.message.media) if event.message.media else None
        await save_message(
            msg_id=msg_id,
            from_id=from_id,
            chat_id=chat_id,
            type=(await get_chat_type(event)).value,
            msg_text=event.message.text,
            media=media,
            noforwards=noforwards,
            self_destructing=self_destructing,
            created_at=datetime.now(timezone.utc),
            edited_at=edited_at,
        )


def get_sender_id(message) -> int:
    from_id = 0
    if isinstance(message.peer_id, PeerUser):
        from_id = MY_ID if message.out else message.peer_id.user_id
    elif isinstance(message.peer_id, (PeerChannel, PeerChat)):
        if isinstance(message.from_id, PeerUser):
            from_id = message.from_id.user_id
        if isinstance(message.from_id, PeerChannel):
            from_id = message.from_id.channel_id

    return from_id


async def load_messages_from_event(
    event: Union[MessageDeleted.Event, MessageEdited.Event, UpdateReadMessagesContents]
) -> List[DbMessage]:
    ids: List[int] = []
    if isinstance(event, MessageDeleted.Event):
        ids = event.deleted_ids[: settings.rate_limit_num_messages]
    if isinstance(event, UpdateReadMessagesContents):
        ids = event.messages[: settings.rate_limit_num_messages]
    elif isinstance(event, MessageEdited.Event):
        ids = [event.message.id]

    db_results: List[DbMessage] = await get_message_ids_by_event(event, ids)
    messages = []
    for db_result in db_results:
        # skip read messages which are not self-destructing
        if isinstance(event, UpdateReadMessagesContents) and not db_result.self_destructing:
            continue
        messages.append(db_result)

    return messages


async def create_mention(entity_id, chat_msg_id: Optional[int] = None) -> str:
    msg_id = 1 if chat_msg_id is None else chat_msg_id

    if entity_id == 0:
        return "Unknown"

    try:
        entity: Entity = await client.get_entity(entity_id)

        if isinstance(entity, (Channel, Chat)):
            name = entity.title
            chat_id = str(entity_id).replace("-100", "")
            mention = f"[{name}](t.me/c/{chat_id}/{msg_id})"
        else:
            if entity.first_name:
                is_pm = chat_msg_id is not None
                name = (entity.first_name + " " if entity.first_name else "") + (
                    entity.last_name if entity.last_name else ""
                )

                mention = f"[{name}](tg://user?id={entity.id})" + (" #pm" if is_pm else "")
            elif entity.username:
                mention = f"[@{entity.username}](t.me/{entity.username})"
            elif entity.phone:
                mention = entity.phone
            else:
                mention = entity.id
    except Exception as e:
        logging.warning(e)
        mention = str(entity_id)

    return mention


async def edited_deleted_handler(
    event: Union[MessageDeleted.Event, MessageEdited.Event, UpdateReadMessagesContents]
):
    if (
        not isinstance(event, MessageDeleted.Event)
        and not isinstance(event, MessageEdited.Event)
        and not isinstance(event, UpdateReadMessagesContents)
    ):
        return

    if isinstance(event, MessageEdited.Event) and not settings.save_edited_messages:
        return

    # todo: update message text to edited one in db
    messages: List[DbMessage] = await load_messages_from_event(event)

    log_deleted_sender_ids = []

    for message in messages:
        media = pickle.loads(message.media) if message.media else None  # noqa: S301

        if message.from_id in settings.ignored_ids or message.chat_id in settings.ignored_ids:
            return

        mention_sender = await create_mention(message.from_id)
        mention_chat = await create_mention(message.chat_id, message.id)

        log_deleted_sender_ids.append(message.from_id)

        text = ""
        if isinstance(event, (MessageDeleted.Event, UpdateReadMessagesContents)):
            if isinstance(event, MessageDeleted.Event):
                text = f"**Deleted message from: **{mention_sender}\n"
            if isinstance(event, UpdateReadMessagesContents):
                text = f"**Deleted #selfdestructing message from: **{mention_sender}\n"

            text += f"in {mention_chat}\n"

            if message.msg_text:
                text += "**Message:** \n" + message.msg_text
        elif isinstance(event, MessageEdited.Event):
            text = f"**✏Edited message from: **{mention_sender}\n"

            text += f"in {mention_chat}\n"

            if message.msg_text:
                text += f"**Original message:**\n{message.msg_text}\n\n"
            if event.message.text:
                text += f"**Edited message:**\n{event.message.text}"

        is_sticker = (
            hasattr(media, "document")
            and media.document.attributes
            and any(
                isinstance(attr, DocumentAttributeSticker) for attr in media.document.attributes
            )
        )
        is_gif = (
            hasattr(media, "document")
            and media.document.attributes
            and any(
                isinstance(attr, DocumentAttributeAnimated) for attr in media.document.attributes
            )
        )
        is_round_video = (
            hasattr(media, "document")
            and media.document.attributes
            and any(
                isinstance(attr, DocumentAttributeVideo) and attr.round_message is True
                for attr in media.document.attributes
            )
        )
        is_dice = isinstance(media, MessageMediaDice)
        is_instant_view = isinstance(media, MessageMediaWebPage)
        is_game = isinstance(media, MessageMediaGame)
        is_geo = isinstance(media, MessageMediaGeo)
        is_poll = isinstance(media, MessageMediaPoll)
        is_contact = isinstance(media, (MessageMediaContact, Contact))

        with retrieve_media_as_file(
            message.id,
            message.chat_id,
            media,
            message.noforwards or message.self_destructing,
        ) as media_file:
            m = text.replace("\n", " ")
            if (
                is_sticker
                or is_round_video
                or is_dice
                or is_game
                or is_contact
                or is_geo
                or is_poll
            ):
                # sent_msg: Message = await client.send_message(config.LOG_CHAT_ID, file=media_file)
                # await sent_msg.reply(text)
                # print(f"{is_round_video=}")
                logging.info(f"{'<new media file>' if media_file else ''} {m}")
            elif is_instant_view:
                # await client.send_message(config.LOG_CHAT_ID, text)
                logging.info(f"{m}")
            else:
                # await client.send_message(config.LOG_CHAT_ID, text, file=media_file)
                # logging.info(f"media: {media_file} {m}")
                logging.info(f"{'<new media file>' if media_file else ''} {m}")

        if is_gif and config.DELETE_SENT_GIFS_FROM_SAVED:
            await delete_from_saved_gifs(media.document)

        if is_sticker and config.DELETE_SENT_STICKERS_FROM_SAVED:
            await delete_from_saved_stickers(media.document)

    ids = []
    event_verb = "unknown"
    if isinstance(event, MessageDeleted.Event):
        ids = event.deleted_ids
        event_verb = "deleted"
    elif isinstance(event, UpdateReadMessagesContents):
        ids = event.messages
        event_verb = "self destructed"
    elif isinstance(event, MessageEdited.Event):
        ids = [event.message.id]
        event_verb = "edited"

    if len(ids) > config.RATE_LIMIT_NUM_MESSAGES and log_deleted_sender_ids:
        await client.send_message(
            config.LOG_CHAT_ID,
            f"{len(ids)} messages {event_verb}. Logged {config.RATE_LIMIT_NUM_MESSAGES}.",
        )

    logging.info(
        f"Got 1 {event_verb} message. DB has {len(messages)}. "
        f"Users: {', '.join(str(_id) for _id in log_deleted_sender_ids)}"
    )


def get_file_name(media: TypeMessageMedia) -> str:
    if not media:
        return ""

    if isinstance(media, (MessageMediaPhoto, Photo)):
        return "photo.jpg"
    if isinstance(media, (MessageMediaContact, Contact)):
        return "contact.vcf"

    for attr in media.document.attributes:
        if isinstance(attr, DocumentAttributeFilename) and hasattr(attr, "file_name"):
            return attr.file_name

    try:
        mime_type = media.document.mime_type
    except (NameError, AttributeError):
        mime_type = None

    if mime_type == "audio/ogg":
        return "audio.ogg"
    if mime_type == "video/mp4":
        return "video.mp4"

    return "file"


async def save_restricted_msg(link: str):
    if link.startswith("tg://"):
        parts = re.findall(r"\d+", link)
        if len(parts) == 2:
            chat_id = int(parts[0])
            msg_id = int(parts[1])
        else:
            await client.send_message(config.LOG_CHAT_ID, f"Could not parse link: {link}")
            return
    else:
        parts = link.split("/")
        msg_id = int(parts[-1])
        chat_id = int(parts[-2]) if parts[-2].isdigit() else parts[-2]

    msg_list = await client.get_messages(chat_id, ids=[msg_id], limit=1)
    msg: Message = msg_list[0]
    chat_id = msg.chat_id
    from_id = get_sender_id(msg)

    mention_sender = await create_mention(from_id)
    mention_chat = await create_mention(chat_id, msg_id)

    text = f"**↗️Saved message from: **{mention_sender}\n"

    text += f"in {mention_chat}\n"

    if msg.text:
        text += "**Message:** \n" + msg.text

    try:
        if msg.media:
            await save_media_as_file(msg)
            with retrieve_media_as_file(msg_id, chat_id, msg.media, True) as f:
                await client.send_message("me", text, file=f)
        else:
            await client.send_message("me", text)
    except Exception as e:
        await client.send_message(settings.log_chat_id, str(e))


async def save_media_as_file(msg: Message):
    msg_id = msg.id
    chat_id = msg.chat_id

    if msg.media:
        if msg.file and msg.file.size > settings.max_in_memory_file_size:
            raise Exception(f"File too large to save ({msg.file.size} bytes)")
        file_path = f"media/{msg_id}_{chat_id}"

        with encryption.encrypted(file_path) as f:
            await client.download_media(msg.media, f)


@contextmanager
def retrieve_media_as_file(msg_id: int, chat_id: int, media, noforwards: bool):
    file_name = get_file_name(media)
    file_path = f"media/{msg_id}_{chat_id}"

    if (
        noforwards
        and not isinstance(media, MessageMediaGeo)
        and not isinstance(media, MessageMediaPoll)
    ):
        with encryption.decrypted(file_path) as f:
            f.name = file_name
            yield f
    else:
        yield media


async def delete_from_saved_gifs(gif: Document):
    await client(
        SaveGifRequest(
            id=InputDocument(
                id=gif.id, access_hash=gif.access_hash, file_reference=gif.file_reference
            ),
            unsave=True,
        )
    )


async def delete_from_saved_stickers(sticker: Document):
    await client(
        SaveRecentStickerRequest(
            id=InputDocument(
                id=sticker.id,
                access_hash=sticker.access_hash,
                file_reference=sticker.file_reference,
            ),
            unsave=True,
        )
    )


async def delete_expired_messages() -> None:
    while True:
        now = datetime.now(timezone.utc)
        await delete_expired_messages_from_db(current_time=now)

        # todo: save group/channel label in file name

        num_files_deleted = 0
        file_persist_days = max(
            settings.persist_time_in_days_group, settings.persist_time_in_days_channel
        )
        for dirpath, dirnames, filenames in os.walk("../media"):
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                modified_time = datetime.fromtimestamp(os.path.getmtime(file_path), timezone.utc)
                expiry_time = now - timedelta(days=file_persist_days)
                if modified_time < expiry_time:
                    os.unlink(file_path)
                    num_files_deleted += 1

        if num_files_deleted > 0:
            logging.info(f"Deleted {num_files_deleted} expired files")

        await asyncio.sleep(300)


async def init() -> None:
    global MY_ID

    if not os.path.exists("../db"):
        os.mkdir("../db")
    if not os.path.exists("../media"):
        os.mkdir("../media")

    await register_models()

    if settings.debug_mode:
        logging.basicConfig(level="INFO")
    else:
        logging.basicConfig(level="WARNING")

    settings.ignored_ids.add(settings.log_chat_id)

    MY_ID = (await client.get_me()).id

    client.add_event_handler(
        new_message_handler,
        events.NewMessage(incoming=True, outgoing=settings.listen_outgoing_messages),
    )
    client.add_event_handler(new_message_handler, events.MessageEdited())
    client.add_event_handler(edited_deleted_handler, events.MessageEdited())
    client.add_event_handler(edited_deleted_handler, events.MessageDeleted())
    client.add_event_handler(edited_deleted_handler)
    # client.add_event_handler(edited_deleted_handler,
    #                          events.MessageRead(True))
    # doesnt work for self destructs

    await delete_expired_messages()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)

    with TelegramClient(
        settings.session_name, settings.api_id, settings.api_hash.get_secret_value()
    ) as client:
        client.loop.run_until_complete(init())
