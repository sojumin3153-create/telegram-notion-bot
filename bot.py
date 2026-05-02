import os
import re
import time
import json
import threading
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
ALLOWED_GROUP_ID = int(os.getenv("ALLOWED_GROUP_ID"))
UPLOADER_USER_ID = int(os.getenv("UPLOADER_USER_ID", "8331369727"))

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
NOTION_API = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}
NOTION_DB_URL = f"https://app.notion.com/p/{DATABASE_ID.replace('-', '')}" if DATABASE_ID else ""
KST = timezone(timedelta(hours=9))

# 미완료 카드 추적: {chat_id: {page_id: [message_id, ...]}}
# 한 카드는 1개 또는 2개 메시지(앨범+버튼) 구성
pending_cards = {}

# 미디어 그룹 버퍼 (동시에 여러 사진 보낼 때 묶어서 처리)
media_group_buffer = {}
media_group_lock = threading.Lock()
MEDIA_GROUP_DELAY = 3.0  # 초


def register_card(chat_id, page_id, message_ids):
    pending_cards.setdefault(chat_id, {})[page_id] = list(message_ids)


def unregister_card(chat_id, page_id):
    if chat_id in pending_cards:
        pending_cards[chat_id].pop(page_id, None)


def get_card_messages(chat_id, page_id):
    return pending_cards.get(chat_id, {}).get(page_id, [])


def delete_and_unregister_card(chat_id, page_id):
    msgs = get_card_messages(chat_id, page_id)
    for mid in msgs:
        delete_message(chat_id, mid)
    unregister_card(chat_id, page_id)


def get_all_pending_message_ids(chat_id):
    ids = []
    for msgs in pending_cards.get(chat_id, {}).values():
        ids.extend(msgs)
    return ids


def clear_all_pending_cards(chat_id):
    for mid in get_all_pending_message_ids(chat_id):
        delete_message(chat_id, mid)
    pending_cards[chat_id] = {}


def get_updates(offset=None):
    params = {"timeout": 30}
    if offset is not None:
        params["offset"] = offset
    return requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=60).json()


def extract_url(text):
    if not text:
        return None
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else None


URGENT_PATTERN = re.compile(r"(?:🚨+|#긴급|\[긴급\]|긴급:)", re.IGNORECASE)


def is_urgent(text):
    return bool(text and URGENT_PATTERN.search(text))


def strip_urgent_markers(text):
    if not text:
        return text
    return URGENT_PATTERN.sub("", text).strip()


def parse_caption(text):
    """캡션에서 URL과 긴급 마커를 제거한 전체 텍스트를 비고로 반환."""
    if not text:
        return ""
    cleaned = re.sub(r"https?://\S+", "", text)
    cleaned = URGENT_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
    return cleaned


def make_title():
    """현재 한국시간 기준 날짜 + 시간을 제목으로 사용."""
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def get_telegram_file_url(file_id):
    res = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
    if not res.get("ok"):
        return None
    file_path = res["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"


def get_media_from_message(message):
    """메시지에서 사진/영상 URL을 추출. [(type, url), ...] 반환."""
    items = []
    photos = message.get("photo", [])
    if photos:
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        url = get_telegram_file_url(largest["file_id"])
        if url:
            items.append(("photo", url))
    video = message.get("video")
    if video:
        url = get_telegram_file_url(video["file_id"])
        if url:
            items.append(("video", url))
    return items


def _normalize_media(media):
    """media가 [(type, url), ...] 또는 URL 문자열/리스트도 받게 정규화."""
    if isinstance(media, str):
        return [("photo", media)]
    if isinstance(media, list):
        normalized = []
        for item in media:
            if isinstance(item, str):
                normalized.append(("photo", item))
            elif isinstance(item, tuple) and len(item) == 2:
                normalized.append(item)
        return normalized
    return []


def save_to_notion(title, link, media, note="", urgent=False):
    """media: [(type, url), ...]. type은 'photo' 또는 'video'."""
    media_items = _normalize_media(media)

    properties = {
        "날짜": {"title": [{"text": {"content": title}}]},
        "상태": {"select": {"name": "소재등록"}},
        "우선순위": {"select": {"name": "긴급" if urgent else "보통"}},
    }
    if link:
        properties["참고 링크"] = {"url": link}
    if media_items:
        files = []
        for i, (mtype, url) in enumerate(media_items):
            ext = "mp4" if mtype == "video" else "jpg"
            prefix = "telegram_video" if mtype == "video" else "telegram_photo"
            files.append(
                {
                    "name": f"{prefix}_{i + 1}.{ext}",
                    "type": "external",
                    "external": {"url": url},
                }
            )
        properties["사진"] = {"files": files}
    if note:
        properties["비고"] = {"rich_text": [{"text": {"content": note}}]}

    # 페이지 본문에 이미지/영상 블록 추가
    children = []
    for mtype, url in media_items:
        block_type = "video" if mtype == "video" else "image"
        children.append(
            {
                "object": "block",
                "type": block_type,
                block_type: {"type": "external", "external": {"url": url}},
            }
        )

    body = {"parent": {"database_id": DATABASE_ID}, "properties": properties}
    if children:
        body["children"] = children

    res = requests.post(f"{NOTION_API}/pages", headers=NOTION_HEADERS, json=body)
    if res.status_code == 200:
        return True, res.json()["id"]
    return False, res.text


def update_notion_status(page_id, status):
    body = {"properties": {"상태": {"select": {"name": status}}}}
    res = requests.patch(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS, json=body)
    return res.status_code == 200


def fetch_notion_page(page_id):
    res = requests.get(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS)
    if res.status_code != 200:
        return None
    return res.json()


def update_notion_page_photos(page_id, media, new_link=None, new_note=None, has_new_text=False):
    """기존 페이지의 사진/영상 교체 + 본문 미디어 블록 재구성."""
    media_items = _normalize_media(media)
    properties = {}
    if media_items:
        files = []
        for i, (mtype, url) in enumerate(media_items):
            ext = "mp4" if mtype == "video" else "jpg"
            prefix = "telegram_video" if mtype == "video" else "telegram_photo"
            files.append(
                {
                    "name": f"{prefix}_{i + 1}.{ext}",
                    "type": "external",
                    "external": {"url": url},
                }
            )
        properties["사진"] = {"files": files}
    if has_new_text:
        properties["참고 링크"] = {"url": new_link} if new_link else {"url": None}
        properties["비고"] = (
            {"rich_text": [{"text": {"content": new_note}}]} if new_note else {"rich_text": []}
        )

    if properties:
        res = requests.patch(
            f"{NOTION_API}/pages/{page_id}",
            headers=NOTION_HEADERS,
            json={"properties": properties},
        )
        if res.status_code != 200:
            return False

    # 본문에서 기존 이미지/영상 블록 삭제 후 새 미디어 추가
    list_res = requests.get(
        f"{NOTION_API}/blocks/{page_id}/children", headers=NOTION_HEADERS
    )
    if list_res.status_code == 200:
        for block in list_res.json().get("results", []):
            if block.get("type") in ("image", "video"):
                requests.delete(
                    f"{NOTION_API}/blocks/{block['id']}", headers=NOTION_HEADERS
                )

    if media_items:
        children = []
        for mtype, url in media_items:
            block_type = "video" if mtype == "video" else "image"
            children.append(
                {
                    "object": "block",
                    "type": block_type,
                    block_type: {"type": "external", "external": {"url": url}},
                }
            )
        requests.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            json={"children": children},
        )

    return True


def extract_page_id_from_message(message):
    """봇 카드 메시지의 inline 버튼 callback_data에서 page_id 추출."""
    if not message:
        return None
    reply_markup = message.get("reply_markup", {})
    for row in reply_markup.get("inline_keyboard", []):
        for btn in row:
            data = btn.get("callback_data", "")
            for prefix in ("complete:", "hold:", "undo:"):
                if data.startswith(prefix):
                    return data.split(":", 1)[1]
    return None


def get_pending_items():
    body = {
        "filter": {
            "and": [
                {"property": "상태", "select": {"does_not_equal": "업로드완료"}},
                {"property": "상태", "select": {"does_not_equal": "보류"}},
            ]
        },
        "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
    }
    res = requests.post(
        f"{NOTION_API}/databases/{DATABASE_ID}/query",
        headers=NOTION_HEADERS,
        json=body,
    )
    if res.status_code != 200:
        return None
    items = res.json().get("results", [])
    # 긴급 항목을 위로 정렬
    def urgent_key(item):
        prio = item.get("properties", {}).get("우선순위", {}).get("select")
        is_urgent_item = prio and prio.get("name") == "긴급"
        return (0 if is_urgent_item else 1, item.get("created_time", ""))
    items.sort(key=urgent_key)
    return items


def get_pending_count():
    items = get_pending_items()
    return len(items) if items is not None else None


def extract_item_data(item):
    page_id = item["id"]
    props = item["properties"]

    title_prop = props.get("날짜", {}).get("title", [])
    title = title_prop[0]["plain_text"] if title_prop else "(제목 없음)"

    link = props.get("참고 링크", {}).get("url") or ""

    note_prop = props.get("비고", {}).get("rich_text", [])
    note = note_prop[0]["plain_text"] if note_prop else ""

    prio = props.get("우선순위", {}).get("select")
    urgent = bool(prio and prio.get("name") == "긴급")

    media_url = None
    media_type = "photo"
    files = props.get("사진", {}).get("files", [])
    if files:
        f = files[0]
        name = f.get("name", "").lower()
        if name.endswith((".mp4", ".mov", ".webm")) or "video" in name:
            media_type = "video"
        if f.get("type") == "external":
            media_url = f.get("external", {}).get("url")
        elif f.get("type") == "file":
            media_url = f.get("file", {}).get("url")

    return {
        "page_id": page_id,
        "title": title,
        "link": link,
        "note": note,
        "urgent": urgent,
        "media_url": media_url,
        "media_type": media_type,
    }


def send_photo(chat_id, photo_url, caption, reply_markup=None):
    """텔레그램 URL인 경우 다운로드 후 재업로드."""
    try:
        if photo_url.startswith("https://api.telegram.org/file/"):
            img = requests.get(photo_url, timeout=30)
            if img.status_code != 200:
                return None
            files = {"photo": ("photo.jpg", img.content)}
            data = {"chat_id": str(chat_id), "caption": caption}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            res = requests.post(f"{TELEGRAM_API}/sendPhoto", files=files, data=data, timeout=60)
        else:
            payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            res = requests.post(f"{TELEGRAM_API}/sendPhoto", json=payload, timeout=60)
        return res.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"send_photo error: {e}")
        return None


def send_video(chat_id, video_url, caption, reply_markup=None):
    """영상 전송 (텔레그램 URL은 다운로드 후 재업로드)."""
    try:
        if video_url.startswith("https://api.telegram.org/file/"):
            vid = requests.get(video_url, timeout=120)
            if vid.status_code != 200:
                return None
            files = {"video": ("video.mp4", vid.content, "video/mp4")}
            data = {"chat_id": str(chat_id), "caption": caption}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            res = requests.post(f"{TELEGRAM_API}/sendVideo", files=files, data=data, timeout=300)
        else:
            payload = {"chat_id": chat_id, "video": video_url, "caption": caption}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            res = requests.post(f"{TELEGRAM_API}/sendVideo", json=payload, timeout=300)
        return res.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"send_video error: {e}")
        return None


def send_media_group(chat_id, media_items, caption):
    """여러 사진/영상을 album으로 전송. 첫 항목에만 캡션. 첫 메시지 ID 반환."""
    try:
        files = {}
        media = []
        for i, (mtype, url) in enumerate(media_items[:10]):  # Telegram media group 최대 10개
            data_res = requests.get(url, timeout=120)
            if data_res.status_code != 200:
                continue
            attach_name = f"m{i}"
            ext = "mp4" if mtype == "video" else "jpg"
            mime = "video/mp4" if mtype == "video" else "image/jpeg"
            files[attach_name] = (f"m{i}.{ext}", data_res.content, mime)
            item = {"type": mtype, "media": f"attach://{attach_name}"}
            if i == 0:
                item["caption"] = caption
            media.append(item)
        if not media:
            return None
        data = {"chat_id": str(chat_id), "media": json.dumps(media)}
        res = requests.post(f"{TELEGRAM_API}/sendMediaGroup", files=files, data=data, timeout=300)
        result = res.json().get("result", [])
        return result[0]["message_id"] if result else None
    except Exception as e:
        print(f"send_media_group error: {e}")
        return None


def send_pending_list(chat_id, max_items=15):
    # 이전 미완료 카드 모두 삭제
    clear_all_pending_cards(chat_id)

    items = get_pending_items()

    if items is None:
        send_message(chat_id, "❌ Notion 조회 실패")
        return

    count = len(items)
    if count == 0:
        send_message(chat_id, "🎉 미완료 게시물이 없습니다!")
        return

    send_message(chat_id, f"📋 미완료 게시물 {count}건 (오래된 순)")

    for item in items[:max_items]:
        data = extract_item_data(item)
        caption_parts = []
        if data["urgent"]:
            caption_parts.append("🚨 긴급")
        caption_parts.append(f"📅 {data['title']}")
        if data["note"]:
            caption_parts.append(f"📝 {data['note']}")
        if data["link"]:
            caption_parts.append(f"🔗 {data['link']}")
        caption = "\n".join(caption_parts)

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 업로드 완료", "callback_data": f"complete:{data['page_id']}"},
                    {"text": "🚫 보류", "callback_data": f"hold:{data['page_id']}"},
                ]
            ]
        }

        if data["media_url"]:
            if data["media_type"] == "video":
                mid = send_video(chat_id, data["media_url"], caption, keyboard)
            else:
                mid = send_photo(chat_id, data["media_url"], caption, keyboard)
        else:
            mid = send_message(chat_id, caption, reply_markup=keyboard)
        if mid:
            register_card(chat_id, data["page_id"], [mid])

    if count > max_items:
        send_message(chat_id, f"...외 {count - max_items}건 더 있음 (Notion에서 확인)")


def send_message(chat_id, text, reply_to_message_id=None, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    res = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)
    try:
        return res.json().get("result", {}).get("message_id")
    except Exception:
        return None


def pin_message(chat_id, message_id, disable_notification=False):
    try:
        requests.post(
            f"{TELEGRAM_API}/pinChatMessage",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": disable_notification,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"pin_message error: {e}")


def delete_message(chat_id, message_id):
    try:
        requests.post(
            f"{TELEGRAM_API}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=10,
        )
    except Exception:
        pass


def send_complete_button_reply(chat_id, text, reply_to_message_id, page_id):
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ 업로드 완료", "callback_data": f"complete:{page_id}"},
                {"text": "🚫 보류", "callback_data": f"hold:{page_id}"},
            ]
        ]
    }
    return send_message(chat_id, text, reply_to_message_id, keyboard)


def build_card_caption(title, media_count, note, link, has_video=False, urgent=False):
    parts = []
    if urgent:
        parts.append("🚨🚨🚨 긴급 콘텐츠 🚨🚨🚨")
        parts.append("")
    parts.append(f"📅 {title}")
    if media_count > 1:
        label = "미디어" if has_video else "사진"
        parts.append(f"🖼 {label} {media_count}개")
    if note:
        parts.append(f"📝 {note}")
    if link:
        parts.append(f"🔗 {link}")
    return "\n".join(parts)


def send_card(chat_id, media_items, caption_body, page_id, reply_to_message_id=None, urgent=False):
    """봇 카드 전송. media_items: [(type, url), ...]. 보낸 메시지 ID 리스트 반환."""
    media_items = _normalize_media(media_items)
    media_count = len(media_items)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ 업로드 완료", "callback_data": f"complete:{page_id}"},
                {"text": "🚫 보류", "callback_data": f"hold:{page_id}"},
            ]
        ]
    }
    footer = "⚡ 즉시 인스타 업로드 후 버튼 눌러주세요👇" if urgent else "인스타에 올린 후 아래 버튼 눌러주세요👇"

    sent_ids = []
    if media_count > 1:
        album_id = send_media_group(chat_id, media_items, caption_body)
        if album_id:
            sent_ids.append(album_id)
        button_id = send_message(chat_id, footer, reply_markup=keyboard)
        if button_id:
            sent_ids.append(button_id)
    elif media_count == 1:
        mtype, url = media_items[0]
        full_caption = caption_body + "\n\n" + footer
        if mtype == "video":
            sid = send_video(chat_id, url, full_caption, keyboard)
        else:
            sid = send_photo(chat_id, url, full_caption, keyboard)
        if sid:
            sent_ids.append(sid)
    else:
        full_caption = caption_body + "\n\n" + footer
        sid = send_message(chat_id, full_caption, reply_to_message_id, keyboard)
        if sid:
            sent_ids.append(sid)

    # 긴급이면 핀 + 멘션 알림
    if urgent and sent_ids:
        first_msg_id = sent_ids[0]
        pin_message(chat_id, first_msg_id)
        mention_html = f'🚨 <a href="tg://user?id={UPLOADER_USER_ID}">긴급</a> 콘텐츠 즉시 확인!'
        alert_id = send_message(chat_id, mention_html, parse_mode="HTML")
        if alert_id:
            sent_ids.append(alert_id)

    return sent_ids


def edit_existing_entry(chat_id, page_id, media_items, text, original_message_ids, old_bot_message):
    """봇 카드에 답장으로 새 사진/영상을 보냈을 때 - 기존 항목 수정."""
    media_items = _normalize_media(media_items)
    new_link = extract_url(text)
    new_note = parse_caption(text)
    has_new_text = bool(text and text.strip())

    page_data = fetch_notion_page(page_id)
    title = "(제목 없음)"
    existing_link = ""
    existing_note = ""
    if page_data:
        props = page_data.get("properties", {})
        title_prop = props.get("날짜", {}).get("title", [])
        if title_prop:
            title = title_prop[0]["plain_text"]
        existing_link = props.get("참고 링크", {}).get("url") or ""
        note_arr = props.get("비고", {}).get("rich_text", [])
        if note_arr:
            existing_note = note_arr[0]["plain_text"]

    success = update_notion_page_photos(page_id, media_items, new_link, new_note, has_new_text)
    if not success:
        send_message(chat_id, "❌ 수정 실패", original_message_ids[0])
        return

    show_link = new_link if has_new_text else existing_link
    show_note = new_note if has_new_text else existing_note
    has_video = any(m[0] == "video" for m in media_items)

    delete_and_unregister_card(chat_id, page_id)
    if old_bot_message:
        delete_message(chat_id, old_bot_message["message_id"])

    for mid in original_message_ids:
        delete_message(chat_id, mid)

    caption_body = build_card_caption(title, len(media_items), show_note, show_link, has_video) + "\n\n✏️ 수정됨"
    sent_ids = send_card(chat_id, media_items, caption_body, page_id)
    register_card(chat_id, page_id, sent_ids)


def save_and_reply(chat_id, media_items, text, original_message_ids):
    """사진/영상 또는 링크를 Notion에 저장하고 봇이 카드로 답장."""
    media_items = _normalize_media(media_items)
    urgent = is_urgent(text)
    link = extract_url(text)
    note = parse_caption(text)
    title = make_title()

    if not media_items and not link:
        return

    success, result = save_to_notion(title, link, media_items, note, urgent=urgent)
    if not success:
        print(f"Notion error: {result}")
        if original_message_ids:
            send_message(chat_id, "❌ 저장 실패", original_message_ids[0])
        return

    page_id = result
    media_count = len(media_items)
    has_video = any(m[0] == "video" for m in media_items)
    caption_body = build_card_caption(title, media_count, note, link, has_video, urgent)

    reply_to = original_message_ids[0] if original_message_ids else None
    sent_ids = send_card(chat_id, media_items, caption_body, page_id, reply_to, urgent=urgent)

    for mid in original_message_ids:
        delete_message(chat_id, mid)

    register_card(chat_id, page_id, sent_ids)


def process_media_group(media_group_id):
    with media_group_lock:
        group_data = media_group_buffer.pop(media_group_id, None)
    if not group_data:
        return

    messages = group_data["messages"]
    if not messages:
        return

    chat_id = messages[0]["chat"]["id"]

    text = ""
    for msg in messages:
        if msg.get("caption"):
            text = msg["caption"]
            break

    # 모든 사진/영상 URL 수집
    media_items = []
    for msg in messages:
        media_items.extend(get_media_from_message(msg))

    message_ids = [m["message_id"] for m in messages]

    edit_page_id = None
    old_bot_message = None
    for msg in messages:
        reply_to = msg.get("reply_to_message")
        if reply_to and reply_to.get("from", {}).get("is_bot"):
            pid = extract_page_id_from_message(reply_to)
            if pid:
                edit_page_id = pid
                old_bot_message = reply_to
                break

    if edit_page_id:
        edit_existing_entry(chat_id, edit_page_id, media_items, text, message_ids, old_bot_message)
    else:
        save_and_reply(chat_id, media_items, text, message_ids)


def buffer_media_group(media_group_id, message):
    with media_group_lock:
        if media_group_id not in media_group_buffer:
            media_group_buffer[media_group_id] = {"messages": [], "timer": None}
        entry = media_group_buffer[media_group_id]
        if entry["timer"]:
            entry["timer"].cancel()
        entry["messages"].append(message)
        timer = threading.Timer(MEDIA_GROUP_DELAY, process_media_group, args=[media_group_id])
        timer.daemon = True
        timer.start()
        entry["timer"] = timer


def handle_message(message):
    chat_id = message["chat"]["id"]
    if chat_id != ALLOWED_GROUP_ID:
        return

    message_id = message["message_id"]
    text = message.get("caption") or message.get("text") or ""
    photos = message.get("photo", [])
    video = message.get("video")
    has_media = bool(photos or video)
    media_group_id = message.get("media_group_id")

    # 슬래시 명령어 처리
    if text and not has_media:
        cmd = text.strip().split()[0].lower()
        cmd = cmd.split("@")[0]
        if cmd in ("/대기", "/list", "/pending", "/start"):
            delete_message(chat_id, message_id)
            send_pending_list(chat_id)
            return
        if cmd in ("/도움말", "/help"):
            help_text = (
                "🤖 봇 명령어\n\n"
                "/대기 - 미완료 게시물 목록 보기\n"
                "/도움말 - 이 메시지\n\n"
                "사진/영상 + 캡션을 보내면 자동으로 Notion에 저장됩니다."
            )
            send_message(chat_id, help_text)
            return

    if not has_media and not text:
        return

    if media_group_id:
        buffer_media_group(media_group_id, message)
        return

    # 단일 메시지: 사진/영상 추출
    media_items = get_media_from_message(message)

    # 봇 카드에 답장으로 미디어 보낸 경우 → 수정 모드
    reply_to = message.get("reply_to_message")
    if reply_to and reply_to.get("from", {}).get("is_bot") and media_items:
        edit_page_id = extract_page_id_from_message(reply_to)
        if edit_page_id:
            edit_existing_entry(chat_id, edit_page_id, media_items, text, [message_id], reply_to)
            return

    save_and_reply(chat_id, media_items, text, [message_id])


def edit_message(chat_id, message_id, new_text, has_caption, reply_markup=None):
    """텍스트 메시지면 editMessageText, 사진 메시지면 editMessageCaption."""
    endpoint = "editMessageCaption" if has_caption else "editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id}
    if has_caption:
        payload["caption"] = new_text
    else:
        payload["text"] = new_text
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TELEGRAM_API}/{endpoint}", json=payload)


def handle_callback_query(callback):
    query_id = callback["id"]
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    user = callback.get("from", {})
    user_name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip()

    has_caption = "caption" in message
    current_text = message.get("caption") if has_caption else message.get("text", "")

    if data.startswith("complete:") or data.startswith("hold:"):
        is_hold = data.startswith("hold:")
        page_id = data.split(":", 1)[1]
        new_status = "보류" if is_hold else "업로드완료"
        success = update_notion_status(page_id, new_status)
        if success:
            toast_text = "🚫 보류 처리됨" if is_hold else "✅ 완료 처리됨"
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": query_id, "text": toast_text},
            )
            # 완료/보류 처리된 카드는 /대기 실행 시 삭제되지 않도록 추적 해제
            unregister_card(chat_id, page_id)
            now_kst = datetime.now(KST).strftime("%m/%d %H:%M")
            label = "🚫" if is_hold else "✅"
            action = "보류 처리" if is_hold else "업로드 완료"
            new_text = f"{current_text}\n\n{label} {user_name}님이 {action} ({now_kst})"
            keyboard = {
                "inline_keyboard": [
                    [{"text": "↩ 되돌리기", "callback_data": f"undo:{page_id}"}]
                ]
            }
            edit_message(chat_id, message_id, new_text, has_caption, keyboard)
        else:
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={
                    "callback_query_id": query_id,
                    "text": "❌ 처리 실패",
                    "show_alert": True,
                },
            )

    elif data.startswith("undo:"):
        page_id = data.split(":", 1)[1]
        success = update_notion_status(page_id, "소재등록")
        if success:
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": query_id, "text": "↩ 되돌렸습니다"},
            )
            # 다시 미완료 상태이므로 추적에 추가 (다음 /대기 시 삭제됨)
            register_card(chat_id, page_id, [message_id])
            # "✅ X님이 업로드 완료 (...)" 또는 "🚫 X님이 보류 처리 (...)" 제거
            new_text = re.sub(
                r"\n*[✅🚫] .*?님이 (업로드 완료|보류 처리) \([^)]*\)\s*$",
                "",
                current_text,
            ).rstrip()
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ 업로드 완료", "callback_data": f"complete:{page_id}"},
                        {"text": "🚫 보류", "callback_data": f"hold:{page_id}"},
                    ]
                ]
            }
            edit_message(chat_id, message_id, new_text, has_caption, keyboard)
        else:
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={
                    "callback_query_id": query_id,
                    "text": "❌ 되돌리기 실패",
                    "show_alert": True,
                },
            )


def send_daily_summary():
    count = get_pending_count()
    if count is None:
        print("Daily summary: failed to fetch count")
        return
    if count == 0:
        text = "☀️ 좋은 아침!\n오늘 미완료 게시물이 없습니다 🎉"
    else:
        text = (
            f"☀️ 좋은 아침!\n"
            f"미완료 게시물: {count}건\n\n"
            f"📋 확인하기: {NOTION_DB_URL}"
        )
    send_message(ALLOWED_GROUP_ID, text)


def daily_scheduler():
    """Send daily summary at 9am KST."""
    print("📅 Daily scheduler started (9am KST)")
    while True:
        try:
            now = datetime.now(KST)
            next_run = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run = next_run + timedelta(days=1)
            seconds_until = (next_run - now).total_seconds()
            print(f"Next daily summary: {next_run.isoformat()} (in {int(seconds_until)}s)")
            time.sleep(seconds_until)
            send_daily_summary()
            time.sleep(60)
        except Exception as e:
            print(f"Scheduler error: {e}")
            time.sleep(300)


def main():
    print("🤖 봇 시작됨")
    print(f"   감시 그룹 ID: {ALLOWED_GROUP_ID}")

    scheduler_thread = threading.Thread(target=daily_scheduler, daemon=True)
    scheduler_thread.start()

    offset = None
    while True:
        try:
            data = get_updates(offset)
            if not data.get("ok"):
                print(f"Telegram error: {data}")
                time.sleep(5)
                continue

            for update in data["result"]:
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    handle_callback_query(update["callback_query"])
        except KeyboardInterrupt:
            print("\n봇 종료")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
