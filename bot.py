import os
import re
import time
import json
import threading
import requests
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
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

# /대기 미디어 캐시: {page_id: (media_type, telegram_file_id)}
# 같은 사진을 매번 다운로드/업로드하지 않고 file_id로 재사용
media_cache = {}

# 재고 부족 알림: 재고(대기+보류) ≤ 임계값으로 떨어지면 그룹에 알림
LOW_STOCK_THRESHOLD = 5
last_stock_count = None  # 첫 관측 시 초기화
low_stock_alert_messages = {}  # {chat_id: message_id} — 재고 회복 시 삭제용

# 완료 카드 자동 삭제 추적: {chat_id: {page_id: {"message_id": int, "timer": Timer}}}
# 보류는 계속 보관 (재작업용), 완료만 1시간 후 자동 삭제 (/대기 시 즉시 정리, ↩ 시 취소)
completed_cards = {}
completed_cards_lock = threading.Lock()
COMPLETED_AUTO_DELETE_DELAY = 3600  # 1시간 (초)

# 보류 카드 추적: {chat_id: {page_id: [message_ids]}}
# /복구가 이미 표시 중인 카드를 중복 등록하지 않도록 사용
hold_cards = {}

# 매일 9시 알림: 직전 알림 메시지 ID 추적 (다음날 알림 도착 시 자동 삭제)
last_daily_notification_msg_id = None

# 대본 입력 대기: {chat_id: {prompt_message_id: page_id}}
# 📝 대본 전달 버튼 누르면 Force Reply 프롬프트 발행, 답장 도착 시 page_id 매칭에 사용
pending_script_prompts = {}


def register_card(chat_id, page_id, message_ids):
    pending_cards.setdefault(chat_id, {})[page_id] = list(message_ids)
    notion_mark_visible(page_id, True)


def unregister_card(chat_id, page_id):
    # 카드 상태 전이용 (보류/완료로 갈 때) — 메시지는 채팅에 그대로 있음
    # → Notion 표시중은 건드리지 않음
    if chat_id in pending_cards:
        pending_cards[chat_id].pop(page_id, None)


def get_card_messages(chat_id, page_id):
    return pending_cards.get(chat_id, {}).get(page_id, [])


def delete_and_unregister_card(chat_id, page_id):
    msgs = get_card_messages(chat_id, page_id)
    for mid in msgs:
        delete_message(chat_id, mid)
    unregister_card(chat_id, page_id)
    # 보류 추적도 함께 정리 (/수정으로 보류 카드 교체 시 스테일 방지)
    unregister_hold_card(chat_id, page_id)
    # 잠시 안 보임 표시 (직후 새 카드 register 시 다시 True로 돌아감)
    notion_mark_visible(page_id, False)


def get_all_pending_message_ids(chat_id):
    ids = []
    for msgs in pending_cards.get(chat_id, {}).values():
        ids.extend(msgs)
    return ids


def clear_all_pending_cards(chat_id):
    page_ids_to_clear = list(pending_cards.get(chat_id, {}).keys())
    for mid in get_all_pending_message_ids(chat_id):
        delete_message(chat_id, mid)
    pending_cards[chat_id] = {}
    for pid in page_ids_to_clear:
        notion_mark_visible(pid, False)


def _delete_completed_card_now(chat_id, page_id):
    with completed_cards_lock:
        entry = completed_cards.get(chat_id, {}).pop(page_id, None)
    if entry:
        for mid in entry["message_ids"]:
            delete_message(chat_id, mid)
        notion_mark_visible(page_id, False)


def schedule_completed_deletion(chat_id, page_id, message_ids, delay=COMPLETED_AUTO_DELETE_DELAY):
    """완료 카드(앨범+버튼 등 관련 메시지 전부)를 N초 뒤 자동 삭제 예약."""
    with completed_cards_lock:
        existing = completed_cards.get(chat_id, {}).get(page_id)
        if existing:
            existing["timer"].cancel()
        timer = threading.Timer(delay, _delete_completed_card_now, args=[chat_id, page_id])
        timer.daemon = True
        timer.start()
        completed_cards.setdefault(chat_id, {})[page_id] = {
            "message_ids": list(message_ids),
            "timer": timer,
        }


def cancel_completed_deletion(chat_id, page_id):
    """↩ 되돌리기 / 폐기 시 자동 삭제 타이머 취소."""
    with completed_cards_lock:
        entry = completed_cards.get(chat_id, {}).pop(page_id, None)
    if entry:
        entry["timer"].cancel()


def clear_all_completed_cards(chat_id):
    """/대기 호출 시 완료 카드 일괄 삭제 + 타이머 정리 (보류는 추적 안 됨)."""
    with completed_cards_lock:
        cards = completed_cards.get(chat_id, {})
        page_ids_to_clear = list(cards.keys())
        snapshot = list(cards.values())
        for entry in snapshot:
            entry["timer"].cancel()
        completed_cards[chat_id] = {}
    for entry in snapshot:
        for mid in entry["message_ids"]:
            delete_message(chat_id, mid)
    for pid in page_ids_to_clear:
        notion_mark_visible(pid, False)


def register_hold_card(chat_id, page_id, message_ids):
    """🚫 보류 시 추적 — /복구가 중복 등록하지 않도록."""
    hold_cards.setdefault(chat_id, {})[page_id] = list(message_ids)
    notion_mark_visible(page_id, True)


def unregister_hold_card(chat_id, page_id):
    """상태 전이용 — 메시지는 채팅에 그대로 있음 → Notion은 건드리지 않음."""
    if chat_id in hold_cards:
        hold_cards[chat_id].pop(page_id, None)


def get_visible_page_ids(chat_id):
    """현재 채팅에서 봇이 추적 중인 모든 카드의 page_id (소재등록 + 보류 + 완료대기)."""
    visible = set()
    visible.update(pending_cards.get(chat_id, {}).keys())
    visible.update(hold_cards.get(chat_id, {}).keys())
    with completed_cards_lock:
        visible.update(completed_cards.get(chat_id, {}).keys())
    return visible


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


# 긴급 마커 인식: 🚨, #긴급, [긴급], 긴급:, 또는 단독 단어 "긴급"
# 단독 "긴급"은 앞뒤에 공백/개행/문자열 시작/끝이 와야 함 (예: "긴급재난" 같은 단어 일부는 무시)
URGENT_PATTERN = re.compile(
    r"(?:🚨+|#긴급|\[긴급\]|긴급:|(?:^|\s)긴급(?=\s|$))",
    re.IGNORECASE | re.MULTILINE,
)


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
    """메시지에서 사진/영상을 추출. [(type, url, file_id), ...] 반환.

    url은 getFile 가능(<=20MB)할 때만 채워지고, 초과 시 None.
    file_id는 항상 보존되어 텔레그램 재전송에 사용 가능."""
    items = []
    photos = message.get("photo", [])
    if photos:
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        file_id = largest["file_id"]
        url = get_telegram_file_url(file_id)
        items.append(("photo", url, file_id))
    video = message.get("video")
    if video:
        file_id = video["file_id"]
        url = get_telegram_file_url(file_id)
        items.append(("video", url, file_id))
    return items


def _normalize_media(media):
    """media를 [(type, url, file_id), ...] 형태로 정규화.

    하위 호환:
    - URL 문자열 → ("photo", url, None)
    - 2-튜플 (type, url) → (type, url, None)
    - 3-튜플 (type, url, file_id) → 그대로
    """
    if isinstance(media, str):
        return [("photo", media, None)]
    if isinstance(media, list):
        normalized = []
        for item in media:
            if isinstance(item, str):
                normalized.append(("photo", item, None))
            elif isinstance(item, tuple):
                if len(item) == 3:
                    normalized.append(item)
                elif len(item) == 2:
                    normalized.append((item[0], item[1], None))
        return normalized
    return []


def save_to_notion(title, link, media, note="", urgent=False):
    """media: [(type, url, file_id), ...]. type은 'photo' 또는 'video'."""
    media_items = _normalize_media(media)
    notion_media = [(t, u) for t, u, _ in media_items if u]
    skipped = sum(1 for _, u, _ in media_items if not u)

    if skipped:
        warn = f"⚠️ 영상 {skipped}개는 20MB 초과로 Notion에 첨부되지 않았습니다 (텔레그램 카드에는 정상 표시)"
        note = f"{note}\n{warn}" if note else warn

    properties = {
        "날짜": {"title": [{"text": {"content": title}}]},
        "상태": {"select": {"name": "소재등록"}},
        "우선순위": {"select": {"name": "긴급" if urgent else "보통"}},
    }
    if link:
        properties["참고 링크"] = {"url": link}
    if notion_media:
        files = []
        for i, (mtype, url) in enumerate(notion_media):
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
    for mtype, url in notion_media:
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


def update_notion_script(page_id, script_text):
    """노션 페이지의 '대본' 속성에 텍스트 저장. 2000자씩 나눠서 rich_text 배열로 전달."""
    chunks = [script_text[i:i + 2000] for i in range(0, len(script_text), 2000)] or [""]
    rich_text = [{"text": {"content": c}} for c in chunks]
    try:
        res = requests.patch(
            f"{NOTION_API}/pages/{page_id}",
            headers=NOTION_HEADERS,
            json={"properties": {"대본": {"rich_text": rich_text}}},
            timeout=15,
        )
        return res.status_code == 200
    except Exception as e:
        print(f"update_notion_script error: {e}")
        return False


def archive_notion_page(page_id):
    """페이지를 Notion에서 아카이브(휴지통 이동) — 사실상 삭제."""
    res = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=NOTION_HEADERS,
        json={"archived": True},
    )
    return res.status_code == 200


def notion_mark_visible(page_id, visible):
    """텔레그램에 표시 중인지 Notion '표시중' 체크박스에 기록.
    봇 재시작 후에도 /복구가 중복 등록하지 않도록 영속 추적."""
    try:
        requests.patch(
            f"{NOTION_API}/pages/{page_id}",
            headers=NOTION_HEADERS,
            json={"properties": {"표시중": {"checkbox": bool(visible)}}},
            timeout=10,
        )
    except Exception as e:
        print(f"notion_mark_visible error: {e}")


def fetch_notion_page(page_id):
    res = requests.get(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS)
    if res.status_code != 200:
        return None
    return res.json()


def update_notion_page_photos(page_id, media, new_link=None, new_note=None, has_new_text=False):
    """기존 페이지의 사진/영상 교체 + 본문 미디어 블록 재구성."""
    media_items = _normalize_media(media)
    notion_media = [(t, u) for t, u, _ in media_items if u]
    skipped = sum(1 for _, u, _ in media_items if not u)
    properties = {}
    if notion_media:
        files = []
        for i, (mtype, url) in enumerate(notion_media):
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
        warn = (
            f"⚠️ 영상 {skipped}개는 20MB 초과로 Notion에 첨부되지 않았습니다 (텔레그램 카드에는 정상 표시)"
            if skipped
            else ""
        )
        merged_note = f"{new_note}\n{warn}".strip() if (new_note and warn) else (new_note or warn)
        properties["비고"] = (
            {"rich_text": [{"text": {"content": merged_note}}]}
            if merged_note
            else {"rich_text": []}
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

    if notion_media:
        children = []
        for mtype, url in notion_media:
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
            for prefix in (
                "complete:",
                "hold:",
                "undo:",
                "discard:",
                "confirm_discard:",
                "cancel_discard:",
            ):
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


def get_status_summary():
    """상태별 카운트 반환: {'urgent_pending', 'normal_pending', 'hold', 'completed_today', 'completed_month'}"""
    summary = {
        "urgent_pending": 0,
        "normal_pending": 0,
        "hold": 0,
        "completed_today": 0,
        "completed_month": 0,
    }

    items = get_pending_items()
    if items:
        for item in items:
            prio = item.get("properties", {}).get("우선순위", {}).get("select")
            if prio and prio.get("name") == "긴급":
                summary["urgent_pending"] += 1
            else:
                summary["normal_pending"] += 1

    # 보류 카운트
    hold_body = {
        "filter": {"property": "상태", "select": {"equals": "보류"}},
    }
    hold_res = requests.post(
        f"{NOTION_API}/databases/{DATABASE_ID}/query",
        headers=NOTION_HEADERS,
        json=hold_body,
    )
    if hold_res.status_code == 200:
        summary["hold"] = len(hold_res.json().get("results", []))

    # 오늘/이번 달 업로드 완료 카운트 (KST 기준 자정 ISO 8601로 정확히 지정)
    now_kst = datetime.now(KST)
    today_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    done_body = {
        "filter": {
            "and": [
                {"property": "상태", "select": {"equals": "업로드완료"}},
                {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": today_start}},
            ]
        },
    }
    done_res = requests.post(
        f"{NOTION_API}/databases/{DATABASE_ID}/query",
        headers=NOTION_HEADERS,
        json=done_body,
    )
    if done_res.status_code == 200:
        summary["completed_today"] = len(done_res.json().get("results", []))

    month_body = {
        "filter": {
            "and": [
                {"property": "상태", "select": {"equals": "업로드완료"}},
                {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": month_start}},
            ]
        },
    }
    month_res = requests.post(
        f"{NOTION_API}/databases/{DATABASE_ID}/query",
        headers=NOTION_HEADERS,
        json=month_body,
    )
    if month_res.status_code == 200:
        summary["completed_month"] = len(month_res.json().get("results", []))

    return summary


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

    media_items = []
    files = props.get("사진", {}).get("files", [])
    for f in files:
        name = f.get("name", "").lower()
        is_video = name.endswith((".mp4", ".mov", ".webm")) or "video" in name
        mtype = "video" if is_video else "photo"
        if f.get("type") == "external":
            url = f.get("external", {}).get("url")
        elif f.get("type") == "file":
            url = f.get("file", {}).get("url")
        else:
            url = None
        if url:
            media_items.append((mtype, url, None))

    # /대기 호환용: 첫 번째 미디어만 사용
    if media_items:
        media_type, media_url = media_items[0][0], media_items[0][1]
    else:
        media_type, media_url = "photo", None

    return {
        "page_id": page_id,
        "title": title,
        "link": link,
        "note": note,
        "urgent": urgent,
        "media_url": media_url,
        "media_type": media_type,
        "media_items": media_items,
    }


def send_photo(chat_id, photo_url, caption, reply_markup=None, parse_mode=None, file_id=None):
    """file_id가 있으면 그대로 재전송. 없으면 텔레그램 URL은 다운로드 후 재업로드."""
    try:
        if file_id:
            payload = {"chat_id": chat_id, "photo": file_id, "caption": caption}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            if parse_mode:
                payload["parse_mode"] = parse_mode
            res = requests.post(f"{TELEGRAM_API}/sendPhoto", json=payload, timeout=60)
        elif photo_url and photo_url.startswith("https://api.telegram.org/file/"):
            img = requests.get(photo_url, timeout=30)
            if img.status_code != 200:
                return None
            files = {"photo": ("photo.jpg", img.content)}
            data = {"chat_id": str(chat_id), "caption": caption}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            if parse_mode:
                data["parse_mode"] = parse_mode
            res = requests.post(f"{TELEGRAM_API}/sendPhoto", files=files, data=data, timeout=60)
        elif photo_url:
            payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            if parse_mode:
                payload["parse_mode"] = parse_mode
            res = requests.post(f"{TELEGRAM_API}/sendPhoto", json=payload, timeout=60)
        else:
            return None
        return res.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"send_photo error: {e}")
        return None


def send_video(chat_id, video_url, caption, reply_markup=None, parse_mode=None, file_id=None):
    """file_id가 있으면 그대로 재전송(사이즈 제한 없음).
    없으면 텔레그램 URL은 다운로드 후 재업로드."""
    try:
        if file_id:
            payload = {"chat_id": chat_id, "video": file_id, "caption": caption}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            if parse_mode:
                payload["parse_mode"] = parse_mode
            res = requests.post(f"{TELEGRAM_API}/sendVideo", json=payload, timeout=120)
        elif video_url and video_url.startswith("https://api.telegram.org/file/"):
            vid = requests.get(video_url, timeout=120)
            if vid.status_code != 200:
                return None
            files = {"video": ("video.mp4", vid.content, "video/mp4")}
            data = {"chat_id": str(chat_id), "caption": caption}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            if parse_mode:
                data["parse_mode"] = parse_mode
            res = requests.post(f"{TELEGRAM_API}/sendVideo", files=files, data=data, timeout=300)
        elif video_url:
            payload = {"chat_id": chat_id, "video": video_url, "caption": caption}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            if parse_mode:
                payload["parse_mode"] = parse_mode
            res = requests.post(f"{TELEGRAM_API}/sendVideo", json=payload, timeout=300)
        else:
            return None
        return res.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"send_video error: {e}")
        return None


def send_media_group(chat_id, media_items, caption, parse_mode=None):
    """여러 사진/영상을 album으로 전송. 첫 항목에만 캡션. 전체 메시지 ID 리스트 반환.

    file_id가 있으면 다운로드 없이 그대로 재전송(20MB 초과 영상도 OK).
    file_id가 없으면 URL을 다운로드해서 multipart 업로드."""
    media_items = _normalize_media(media_items)
    try:
        files = {}
        media = []
        for i, (mtype, url, file_id) in enumerate(media_items[:10]):
            if file_id:
                # 텔레그램 → 텔레그램: file_id 직접 사용 (사이즈 제한 없음)
                item = {"type": mtype, "media": file_id}
            elif url:
                data_res = requests.get(url, timeout=120)
                if data_res.status_code != 200:
                    continue
                attach_name = f"m{i}"
                ext = "mp4" if mtype == "video" else "jpg"
                mime = "video/mp4" if mtype == "video" else "image/jpeg"
                files[attach_name] = (f"m{i}.{ext}", data_res.content, mime)
                item = {"type": mtype, "media": f"attach://{attach_name}"}
            else:
                continue
            if not media:  # 첫 유효 항목에만 캡션
                item["caption"] = caption
                if parse_mode:
                    item["parse_mode"] = parse_mode
            media.append(item)
        if not media:
            return []
        data = {"chat_id": str(chat_id), "media": json.dumps(media)}
        if files:
            res = requests.post(f"{TELEGRAM_API}/sendMediaGroup", files=files, data=data, timeout=300)
        else:
            res = requests.post(f"{TELEGRAM_API}/sendMediaGroup", data=data, timeout=120)
        result = res.json().get("result", [])
        return [m["message_id"] for m in result if "message_id" in m]
    except Exception as e:
        print(f"send_media_group error: {e}")
        return []


def schedule_ephemeral_deletion(chat_id, message_id, delay=30):
    """일회성 메시지(예: /개수 응답)를 delay 초 후 자동 삭제."""
    if not message_id:
        return
    t = threading.Timer(delay, delete_message, args=[chat_id, message_id])
    t.daemon = True
    t.start()


def send_status_summary(chat_id):
    """재고 관점 압축형 카운트 표시. 30초 후 자동 삭제 (휘발성)."""
    s = get_status_summary()
    stock = s["normal_pending"] + s["hold"]
    lines = []
    if s["urgent_pending"]:
        lines.append(f"🚨 긴급 미완료: {s['urgent_pending']}건")
    lines.append(f"📦 재고 {stock}건 (대기 {s['normal_pending']} · 보류 {s['hold']})")
    lines.append(f"📤 오늘 {s['completed_today']}건 · 이번 달 {s['completed_month']}건")
    msg_id = send_message(chat_id, "\n".join(lines))
    schedule_ephemeral_deletion(chat_id, msg_id, delay=30)


def cache_invalidate_media(page_id):
    """수정 또는 사진 변경 시 캐시 무효화."""
    media_cache.pop(page_id, None)


def send_pending_media_cached(chat_id, page_id, media_type, media_url, caption, keyboard):
    """/대기·/복구 카드 미디어 전송 — file_id 캐시 사용. 반환: message_id."""
    cached = media_cache.get(page_id)
    if cached and cached[0] == media_type:
        endpoint = "sendVideo" if media_type == "video" else "sendPhoto"
        field = "video" if media_type == "video" else "photo"
        try:
            payload = {
                "chat_id": chat_id,
                field: cached[1],
                "caption": caption,
                "reply_markup": keyboard,
            }
            res = requests.post(f"{TELEGRAM_API}/{endpoint}", json=payload, timeout=30)
            data = res.json()
            if data.get("ok"):
                return data.get("result", {}).get("message_id")
            # 캐시된 file_id가 만료/유효하지 않으면 fall-through
        except Exception as e:
            print(f"send_pending_media_cached cache hit error: {e}")

    # 캐시 미스 또는 무효 — Telegram CDN URL은 자기 자신이 못 받으므로
    # 다운로드 후 multipart 업로드로 재전송. 외부 URL은 직접 전송.
    endpoint = "sendVideo" if media_type == "video" else "sendPhoto"
    field = "video" if media_type == "video" else "photo"

    try:
        if media_url.startswith("https://api.telegram.org/file/"):
            timeout_dl = 120 if media_type == "video" else 30
            timeout_up = 300 if media_type == "video" else 60
            dl_res = requests.get(media_url, timeout=timeout_dl)
            if dl_res.status_code != 200:
                return None
            ext = "mp4" if media_type == "video" else "jpg"
            mime = "video/mp4" if media_type == "video" else "image/jpeg"
            files = {field: (f"file.{ext}", dl_res.content, mime)}
            data = {
                "chat_id": str(chat_id),
                "caption": caption,
                "reply_markup": json.dumps(keyboard),
            }
            res = requests.post(
                f"{TELEGRAM_API}/{endpoint}", files=files, data=data, timeout=timeout_up
            )
        else:
            payload = {
                "chat_id": chat_id,
                field: media_url,
                "caption": caption,
                "reply_markup": keyboard,
            }
            res = requests.post(f"{TELEGRAM_API}/{endpoint}", json=payload, timeout=120)

        result = res.json().get("result", {})
        message_id = result.get("message_id")
        if media_type == "video":
            file_id = result.get("video", {}).get("file_id")
        else:
            photos = result.get("photo", [])
            file_id = photos[-1]["file_id"] if photos else None
        if file_id:
            media_cache[page_id] = (media_type, file_id)
        return message_id
    except Exception as e:
        print(f"send_pending_media_cached send error: {e}")
        return None


def check_low_stock_alert(chat_id):
    """재고가 임계값 이하로 떨어지면 알림, 임계값 초과로 회복되면 알림 삭제."""
    global last_stock_count
    s = get_status_summary()
    new_stock = s["normal_pending"] + s["hold"]

    should_alert = False
    if last_stock_count is None:
        if new_stock <= LOW_STOCK_THRESHOLD:
            should_alert = True
    elif last_stock_count > LOW_STOCK_THRESHOLD and new_stock <= LOW_STOCK_THRESHOLD:
        should_alert = True

    if should_alert:
        msg_id = send_message(
            chat_id,
            f"⚠️ 재고 부족 경고\n"
            f"📦 현재 재고: {new_stock}건\n"
            f"새 콘텐츠 제작이 필요합니다.",
        )
        if msg_id:
            low_stock_alert_messages[chat_id] = msg_id
    elif new_stock > LOW_STOCK_THRESHOLD and chat_id in low_stock_alert_messages:
        # 재고 회복 — 기존 경고 메시지 삭제
        delete_message(chat_id, low_stock_alert_messages.pop(chat_id))

    last_stock_count = new_stock


def search_notion(keyword):
    """비고/참고링크에서 키워드 검색."""
    body = {
        "filter": {
            "or": [
                {"property": "비고", "rich_text": {"contains": keyword}},
                {"property": "참고 링크", "url": {"contains": keyword}},
            ]
        },
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": 20,
    }
    res = requests.post(
        f"{NOTION_API}/databases/{DATABASE_ID}/query",
        headers=NOTION_HEADERS,
        json=body,
    )
    if res.status_code != 200:
        return None
    return res.json().get("results", [])


def send_search_results(chat_id, keyword, max_items=10):
    results = search_notion(keyword)
    if results is None:
        mid = send_message(chat_id, "❌ 검색 실패")
        schedule_ephemeral_deletion(chat_id, mid, delay=30)
        return
    if not results:
        mid = send_message(chat_id, f"🔍 '{keyword}' — 결과 없음")
        schedule_ephemeral_deletion(chat_id, mid, delay=30)
        return

    sent_ids = []
    sent_ids.append(send_message(chat_id, f"🔍 '{keyword}' — {len(results)}건"))

    status_emoji = {"소재등록": "📋", "업로드완료": "✅", "보류": "🚫"}

    for item in results[:max_items]:
        data = extract_item_data(item)
        status_prop = item["properties"].get("상태", {}).get("select")
        status_name = status_prop.get("name") if status_prop else "?"
        emoji = status_emoji.get(status_name, "?")

        parts = [f"{emoji} {status_name}"]
        if data["urgent"]:
            parts.append("🚨 긴급")
        parts.append(f"📅 {data['title']}")
        if data["note"]:
            parts.append(f"📝 {data['note']}")
        if data["link"]:
            parts.append(f"🔗 {data['link']}")
        caption = "\n".join(parts)

        if data["media_url"]:
            if data["media_type"] == "video":
                sent_ids.append(send_video(chat_id, data["media_url"], caption))
            else:
                sent_ids.append(send_photo(chat_id, data["media_url"], caption))
        else:
            sent_ids.append(send_message(chat_id, caption))

    if len(results) > max_items:
        sent_ids.append(
            send_message(chat_id, f"...외 {len(results) - max_items}건 (Notion에서 확인)")
        )

    # 검색 결과는 3분 후 자동 삭제 (사진/링크 확인할 시간 충분)
    for mid in sent_ids:
        schedule_ephemeral_deletion(chat_id, mid, delay=180)


def upgrade_to_urgent(chat_id, page_id, bot_card_message_id):
    """카드를 긴급으로 승격: Notion 우선순위 변경 + 핀 + 멘션."""
    body = {"properties": {"우선순위": {"select": {"name": "긴급"}}}}
    res = requests.patch(f"{NOTION_API}/pages/{page_id}", headers=NOTION_HEADERS, json=body)
    if res.status_code != 200:
        return False
    pin_message(chat_id, bot_card_message_id)
    mention = f'<a href="tg://user?id={UPLOADER_USER_ID}">⚡ Song Won</a>님 즉시 확인!'
    send_message(
        chat_id,
        f"🚨 긴급으로 승격됨\n{mention}",
        reply_to_message_id=bot_card_message_id,
        parse_mode="HTML",
    )
    return True


def send_full_recovery(chat_id, max_items_per_kind=30):
    """전체 복구: 소재등록 + 보류 카드 중 채팅에서 사라진 것만 다시 등록.

    Notion '표시중' 체크박스를 영속 추적 신호로 사용 → 봇 재시작에도 안전."""
    body = {
        "filter": {
            "and": [
                {"property": "상태", "select": {"does_not_equal": "업로드완료"}},
                {"property": "표시중", "checkbox": {"equals": False}},
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
        send_message(chat_id, "❌ Notion 조회 실패")
        return
    items = res.json().get("results", [])

    if not items:
        send_message(chat_id, "🎉 복구할 카드가 없습니다. (모두 표시 중이거나 완료됨)")
        return

    # 메모리 추적과도 비교 (방금 만든 카드는 표시중 업데이트 전일 수 있음)
    already_visible = get_visible_page_ids(chat_id)
    new_items = [i for i in items if i["id"] not in already_visible]
    skipped = len(items) - len(new_items)

    if not new_items:
        send_message(chat_id, f"🎉 복구할 새 카드가 없습니다 ({skipped}건 메모리 스킵)")
        return

    pending_items = []
    hold_items = []
    for item in new_items:
        status_prop = item["properties"].get("상태", {}).get("select")
        status_name = status_prop.get("name") if status_prop else ""
        if status_name == "보류":
            hold_items.append(item)
        else:
            pending_items.append(item)

    # 소재등록 — 긴급 우선, 그 다음 오래된 순
    def urgent_key(item):
        prio = item.get("properties", {}).get("우선순위", {}).get("select")
        is_urgent = prio and prio.get("name") == "긴급"
        return (0 if is_urgent else 1, item.get("created_time", ""))

    pending_items.sort(key=urgent_key)

    header = f"🔄 복구\n📋 소재등록 {len(pending_items)}건 · 🚫 보류 {len(hold_items)}건"
    if skipped:
        header += f"\n(이미 표시 중 {skipped}건 스킵)"
    send_message(chat_id, header)

    # 소재등록 카드 — send_card로 멀티미디어/긴급/버튼 자동 처리
    for item in pending_items[:max_items_per_kind]:
        data = extract_item_data(item)
        media_items_list = data["media_items"]
        has_video = any(m[0] == "video" for m in media_items_list)
        caption_body = build_card_caption(
            data["title"],
            len(media_items_list),
            data["note"],
            data["link"],
            has_video=has_video,
            urgent=data["urgent"],
        )
        sent_ids = send_card(
            chat_id,
            media_items_list,
            caption_body,
            data["page_id"],
            urgent=data["urgent"],
        )
        if sent_ids:
            register_card(chat_id, data["page_id"], sent_ids)

    # 보류 카드 — 보류용 버튼/푸터로 send_card 사용
    for item in hold_items[:max_items_per_kind]:
        data = extract_item_data(item)
        media_items_list = data["media_items"]
        has_video = any(m[0] == "video" for m in media_items_list)
        caption_body = "🚫 보류 상태\n" + build_card_caption(
            data["title"],
            len(media_items_list),
            data["note"],
            data["link"],
            has_video=has_video,
        )
        hold_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "↩ 되돌리기", "callback_data": f"undo:{data['page_id']}"},
                    {"text": "🗑 폐기", "callback_data": f"discard:{data['page_id']}"},
                ]
            ]
        }
        sent_ids = send_card(
            chat_id,
            media_items_list,
            caption_body,
            data["page_id"],
            keyboard=hold_keyboard,
            footer="📌 재작업 또는 폐기",
        )
        if sent_ids:
            register_hold_card(chat_id, data["page_id"], sent_ids)

    truncated = []
    if len(pending_items) > max_items_per_kind:
        truncated.append(f"소재등록 {len(pending_items) - max_items_per_kind}건")
    if len(hold_items) > max_items_per_kind:
        truncated.append(f"보류 {len(hold_items) - max_items_per_kind}건")
    if truncated:
        send_message(chat_id, f"...외 {' · '.join(truncated)} 더 있음 (Notion에서 확인)")


def sync_visible_marker(chat_id):
    """일회용 마이그레이션: 모든 비-완료 카드를 '표시중'으로 마크.
    이미 채팅에 카드들이 떠 있는 상태에서, /복구가 중복으로 다시 보내지 않도록
    Notion '표시중' 체크박스를 일괄 True로 설정."""
    body = {
        "filter": {
            "and": [
                {"property": "상태", "select": {"does_not_equal": "업로드완료"}},
                {"property": "표시중", "checkbox": {"equals": False}},
            ]
        },
        "page_size": 100,
    }
    total = 0
    while True:
        res = requests.post(
            f"{NOTION_API}/databases/{DATABASE_ID}/query",
            headers=NOTION_HEADERS,
            json=body,
        )
        if res.status_code != 200:
            send_message(chat_id, "❌ Notion 조회 실패")
            return
        data = res.json()
        items = data.get("results", [])
        for item in items:
            notion_mark_visible(item["id"], True)
            total += 1
        if not data.get("has_more"):
            break
        body["start_cursor"] = data["next_cursor"]
    send_message(chat_id, f"🔁 동기화 완료: 비-완료 카드 {total}건을 '표시중'으로 마크")


def send_pending_list(chat_id, max_items=15):
    # 이전 미완료 카드 모두 삭제
    clear_all_pending_cards(chat_id)
    # 완료 카드도 함께 정리 (보류는 계속 보관)
    clear_all_completed_cards(chat_id)

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
                    {"text": "✅ 완료", "callback_data": f"complete:{data['page_id']}"},
                    {"text": "🚫 보류", "callback_data": f"hold:{data['page_id']}"},
                    {"text": "📝 대본", "callback_data": f"script:{data['page_id']}"},
                ],
            ]
        }

        if data["media_url"]:
            mid = send_pending_media_cached(
                chat_id,
                data["page_id"],
                data["media_type"],
                data["media_url"],
                caption,
                keyboard,
            )
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
                {"text": "✅ 완료", "callback_data": f"complete:{page_id}"},
                {"text": "🚫 보류", "callback_data": f"hold:{page_id}"},
                {"text": "📝 대본", "callback_data": f"script:{page_id}"},
            ],
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


def send_card(chat_id, media_items, caption_body, page_id, reply_to_message_id=None,
              urgent=False, keyboard=None, footer=None):
    """봇 카드 전송. media_items: [(type, url), ...]. 보낸 메시지 ID 리스트 반환.

    keyboard/footer를 명시하지 않으면 신규 등록(소재등록)용 기본값 사용.
    /복구의 보류 카드는 keyboard/footer를 override해서 호출."""
    media_items = _normalize_media(media_items)
    media_count = len(media_items)
    if keyboard is None:
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 완료", "callback_data": f"complete:{page_id}"},
                    {"text": "🚫 보류", "callback_data": f"hold:{page_id}"},
                    {"text": "📝 대본", "callback_data": f"script:{page_id}"},
                ],
            ]
        }
    if footer is None:
        footer = "⚡ 즉시 인스타 업로드 후 버튼 눌러주세요👇" if urgent else "인스타에 올린 후 아래 버튼 눌러주세요👇"

    # 긴급일 경우 캡션 안에 멘션 임베드
    parse_mode = None
    if urgent:
        # 캡션을 HTML로 보내야 멘션 작동. 기존 캡션 HTML 이스케이프 처리.
        from html import escape as _escape
        escaped_caption = _escape(caption_body)
        escaped_footer = _escape(footer)
        mention = f'<a href="tg://user?id={UPLOADER_USER_ID}">⚡ Song Won</a>님 즉시 확인!'
        caption_body = f"{escaped_caption}\n\n{mention}"
        footer = escaped_footer
        parse_mode = "HTML"

    sent_ids = []
    if media_count > 1:
        album_ids = send_media_group(chat_id, media_items, caption_body, parse_mode=parse_mode)
        sent_ids.extend(album_ids)
        # 버튼은 별도 메시지(앨범에는 인라인 버튼 안 됨)
        button_id = send_message(chat_id, footer, reply_markup=keyboard, parse_mode=parse_mode)
        if button_id:
            sent_ids.append(button_id)
    elif media_count == 1:
        mtype, url, file_id = media_items[0]
        full_caption = caption_body + "\n\n" + footer
        if mtype == "video":
            sid = send_video(chat_id, url, full_caption, keyboard, parse_mode=parse_mode, file_id=file_id)
        else:
            sid = send_photo(chat_id, url, full_caption, keyboard, parse_mode=parse_mode, file_id=file_id)
        if sid:
            sent_ids.append(sid)
    else:
        full_caption = caption_body + "\n\n" + footer
        sid = send_message(chat_id, full_caption, reply_to_message_id, keyboard, parse_mode=parse_mode)
        if sid:
            sent_ids.append(sid)

    # 긴급이면 메시지 핀
    if urgent and sent_ids:
        pin_message(chat_id, sent_ids[0])

    return sent_ids


def edit_existing_entry(chat_id, page_id, media_items, text, original_message_ids, old_bot_message):
    """봇 카드에 답장으로 새 사진/영상을 보냈을 때 - 기존 항목 수정."""
    media_items = _normalize_media(media_items)
    cache_invalidate_media(page_id)  # 사진이 바뀌므로 캐시 무효
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


def handle_script_submission(chat_id, reply_message_id, prompt_message_id, page_id, script_text):
    """📝 대본 전달: 기존 카드 삭제 후 사진 + 라벨(✅/🚫 버튼) + 코드블록 대본 발행."""
    pending_script_prompts.get(chat_id, {}).pop(prompt_message_id, None)
    delete_message(chat_id, prompt_message_id)
    delete_message(chat_id, reply_message_id)

    page = fetch_notion_page(page_id)
    if not page:
        send_message(chat_id, "❌ 카드를 찾을 수 없어 대본을 발행하지 못했습니다.")
        return

    update_notion_script(page_id, script_text)

    data = extract_item_data(page)
    media_items = data["media_items"]

    caption_parts = []
    if data["urgent"]:
        caption_parts.append("🚨 긴급")
        caption_parts.append("")
    caption_parts.append(f"📅 {data['title']}")
    if data["note"]:
        caption_parts.append(f"📝 {data['note']}")
    if data["link"]:
        caption_parts.append(f"🔗 {data['link']}")
    caption_parts.append("")
    caption_parts.append("📝 대본 전달 — 사진/대본을 사용해 직접 업로드해주세요")
    caption_body = "\n".join(caption_parts)

    # 기존 카드 메시지 삭제 (중복 방지) — 이후 새로 register
    delete_and_unregister_card(chat_id, page_id)

    new_message_ids = []
    if len(media_items) > 1:
        album_ids = send_media_group(chat_id, media_items, caption_body)
        new_message_ids.extend(album_ids)
    elif len(media_items) == 1:
        mtype, url, fid = media_items[0]
        if mtype == "video":
            mid = send_video(chat_id, url, caption_body, file_id=fid)
        else:
            mid = send_photo(chat_id, url, caption_body, file_id=fid)
        if mid:
            new_message_ids.append(mid)
    else:
        mid = send_message(chat_id, caption_body)
        if mid:
            new_message_ids.append(mid)

    script_keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ 완료", "callback_data": f"complete:{page_id}"},
                {"text": "🚫 보류", "callback_data": f"hold:{page_id}"},
            ]
        ]
    }
    safe_script = html_escape(script_text)
    script_id = send_message(
        chat_id,
        f"<pre>{safe_script}</pre>",
        reply_markup=script_keyboard,
        parse_mode="HTML",
    )
    if script_id:
        new_message_ids.append(script_id)

    if new_message_ids:
        register_card(chat_id, page_id, new_message_ids)


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

    # 📝 대본 입력 답장 감지 (Force Reply 프롬프트에 대한 응답)
    reply_to_msg = message.get("reply_to_message")
    if reply_to_msg and text and not has_media:
        prompts = pending_script_prompts.get(chat_id, {})
        target_page_id = prompts.get(reply_to_msg.get("message_id"))
        if target_page_id:
            handle_script_submission(chat_id, message_id, reply_to_msg["message_id"], target_page_id, text)
            return

    # 슬래시 명령어 처리
    if text and not has_media:
        stripped = text.strip()
        cmd = stripped.split()[0].lower()
        cmd = cmd.split("@")[0]
        if cmd in ("/대기", "/list", "/pending", "/start"):
            delete_message(chat_id, message_id)
            send_pending_list(chat_id)
            return
        if cmd in ("/복구", "/recover", "/restore"):
            delete_message(chat_id, message_id)
            send_full_recovery(chat_id)
            return
        if cmd in ("/동기화", "/sync"):
            delete_message(chat_id, message_id)
            sync_visible_marker(chat_id)
            return
        if cmd in ("/개수", "/카운트", "/count", "/현황"):
            delete_message(chat_id, message_id)
            send_status_summary(chat_id)
            return
        if cmd in ("/검색", "/search"):
            parts = stripped.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                send_message(chat_id, "사용법: /검색 키워드", message_id)
                return
            keyword = parts[1].strip()
            delete_message(chat_id, message_id)
            send_search_results(chat_id, keyword)
            return
        if cmd in ("/긴급", "/urgent"):
            reply_to = message.get("reply_to_message")
            if not reply_to or not reply_to.get("from", {}).get("is_bot"):
                send_message(chat_id, "❗️ 봇 카드에 답장으로 사용해주세요.", message_id)
                return
            target_page_id = extract_page_id_from_message(reply_to)
            if not target_page_id:
                send_message(chat_id, "❗️ 카드를 식별할 수 없습니다.", message_id)
                return
            delete_message(chat_id, message_id)
            ok = upgrade_to_urgent(chat_id, target_page_id, reply_to["message_id"])
            if not ok:
                send_message(chat_id, "❌ 긴급 승격 실패")
            return
        if cmd in ("/도움말", "/help"):
            delete_message(chat_id, message_id)
            help_text = (
                "🤖 봇 명령어\n\n"
                "/대기 - 미완료 게시물 전체 목록\n"
                "/개수 - 재고 + 발행 카운트\n"
                "/검색 키워드 - 비고/링크 검색\n"
                "/긴급 - (카드에 답장) 긴급으로 승격\n"
                "/복구 - 채팅에서 사라진 소재등록+보류만 다시 등록\n"
                "/동기화 - 일회용: 채팅에 떠 있는 모든 카드를 '표시중'으로 마크\n"
                "/도움말 - 이 메시지\n\n"
                "📤 사진/영상 + 캡션을 보내면 자동 저장\n"
                "✏️ 봇 카드에 새 사진 답장하면 사진 교체\n"
                "🚨 캡션에 '긴급' 또는 '#긴급' 포함 시 긴급 처리\n"
                f"📦 재고 {LOW_STOCK_THRESHOLD}건 이하면 자동 알림\n"
                "🧹 완료 카드는 1시간 후 또는 /대기 시 자동 정리 (보류는 계속 보관)\n"
                "🗑 보류 카드의 폐기 버튼으로 Notion에서도 영구 삭제"
            )
            help_msg_id = send_message(chat_id, help_text)
            # 도움말은 1분 후 자동 삭제 (읽을 시간 충분)
            schedule_ephemeral_deletion(chat_id, help_msg_id, delay=60)
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
            # 카드의 모든 메시지(앨범+버튼) — 완료 시 함께 1시간 후 삭제 예약용
            related_message_ids = list(get_card_messages(chat_id, page_id)) or [message_id]
            if message_id not in related_message_ids:
                related_message_ids.append(message_id)
            # 완료/보류 처리된 카드는 /대기 실행 시 삭제되지 않도록 추적 해제
            unregister_card(chat_id, page_id)
            now_kst = datetime.now(KST).strftime("%m/%d %H:%M")
            label = "🚫" if is_hold else "✅"
            action = "보류 처리" if is_hold else "업로드 완료"
            new_text = f"{current_text}\n\n{label} {user_name}님이 {action} ({now_kst})"
            if is_hold:
                # 보류는 폐기 버튼도 함께 제공
                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "↩ 되돌리기", "callback_data": f"undo:{page_id}"},
                            {"text": "🗑 폐기", "callback_data": f"discard:{page_id}"},
                        ]
                    ]
                }
            else:
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "↩ 되돌리기", "callback_data": f"undo:{page_id}"}]
                    ]
                }
            edit_message(chat_id, message_id, new_text, has_caption, keyboard)
            # 완료/보류 후 재고 변동 체크 (대기→완료 시 재고 -1)
            check_low_stock_alert(chat_id)
            # 완료는 1시간 후 자동 삭제, 보류는 계속 추적 (둘 다 /복구 중복 방지용 추적)
            if is_hold:
                register_hold_card(chat_id, page_id, related_message_ids)
            else:
                schedule_completed_deletion(chat_id, page_id, related_message_ids)
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
                        {"text": "✅ 완료", "callback_data": f"complete:{page_id}"},
                        {"text": "🚫 보류", "callback_data": f"hold:{page_id}"},
                        {"text": "📝 대본", "callback_data": f"script:{page_id}"},
                    ],
                ]
            }
            edit_message(chat_id, message_id, new_text, has_caption, keyboard)
            # 되돌리기로 재고가 회복되었을 수 있으므로 트래커 갱신
            check_low_stock_alert(chat_id)
            # 자동 삭제 예약 취소 + 보류 추적 해제 (어느 상태에서 왔든 안전)
            cancel_completed_deletion(chat_id, page_id)
            unregister_hold_card(chat_id, page_id)
        else:
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={
                    "callback_query_id": query_id,
                    "text": "❌ 되돌리기 실패",
                    "show_alert": True,
                },
            )

    elif data.startswith("script:"):
        page_id = data.split(":", 1)[1]
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": query_id, "text": "📝 대본 입력창을 열었습니다"},
        )
        prompt_text = (
            "📝 이 메시지에 답장으로 대본을 입력해주세요.\n"
            "전송하시면 사진/영상과 함께 대본 메시지가 발행됩니다."
        )
        prompt_id = send_message(
            chat_id,
            prompt_text,
            reply_to_message_id=message_id,
            reply_markup={
                "force_reply": True,
                "input_field_placeholder": "대본을 여기에 입력하세요",
            },
        )
        if prompt_id:
            pending_script_prompts.setdefault(chat_id, {})[prompt_id] = page_id

    elif data.startswith("discard:"):
        # 폐기: 확인 단계 없이 바로 Notion 보관 + 텔레그램 카드(앨범+버튼) 일괄 삭제
        page_id = data.split(":", 1)[1]
        success = archive_notion_page(page_id)
        if success:
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": query_id, "text": "🗑 폐기 완료"},
            )
            msg_ids = list(hold_cards.get(chat_id, {}).get(page_id, []))
            if message_id not in msg_ids:
                msg_ids.append(message_id)
            for mid in msg_ids:
                delete_message(chat_id, mid)
            unregister_card(chat_id, page_id)
            unregister_hold_card(chat_id, page_id)
            cancel_completed_deletion(chat_id, page_id)
            cache_invalidate_media(page_id)
        else:
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={
                    "callback_query_id": query_id,
                    "text": "❌ 폐기 실패",
                    "show_alert": True,
                },
            )


def send_daily_summary():
    global last_daily_notification_msg_id
    count = get_pending_count()
    if count is None:
        print("Daily summary: failed to fetch count")
        return

    # 직전 알림이 있으면 먼저 삭제 (다음날 도착 시점에 어제 알림 정리)
    if last_daily_notification_msg_id:
        delete_message(ALLOWED_GROUP_ID, last_daily_notification_msg_id)
        last_daily_notification_msg_id = None

    if count == 0:
        text = "☀️ 좋은 아침!\n오늘 재고가 없습니다 🎉"
    else:
        text = (
            f"☀️ 좋은 아침!\n"
            f"재고: {count}건\n\n"
            f"📋 확인하기: {NOTION_DB_URL}"
        )
    msg_id = send_message(ALLOWED_GROUP_ID, text)
    if msg_id:
        last_daily_notification_msg_id = msg_id


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
