# ==============================================================================
# 1. 패키지 임포트 및 기본 환경 설정
# ==============================================================================
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from threading import Timer

import cv2
import numpy as np
import yaml
import requests
from flask import Flask, jsonify, render_template, request, send_from_directory
from PIL import Image
import pytesseract

if hasattr(sys, "_MEIPASS"):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

# 작업 디렉토리를 항상 base_dir로 고정 (상대 경로 참조 안전성 보장)
try:
    os.chdir(base_dir)
except Exception:
    pass

template_dir = os.path.join(base_dir, "template")
static_dir = os.path.join(base_dir, "static")
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

APP_VERSION = "1.2.0"

CONFIG_PATH = os.path.join(base_dir, "config.yaml")
BACKUP_DIR = os.path.join(base_dir, "backup")
COSTUMES_DIR = os.path.join(base_dir, "costumes")
UNRECOGNIZED_DIR = os.path.join(base_dir, "unrecognized")
DB_PATH = os.path.join(base_dir, "database.db")

web_logs_queue = []

for directory in [BACKUP_DIR, COSTUMES_DIR, UNRECOGNIZED_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)


def is_ascii_str(s):
    try:
        s.encode("ascii")
        return True
    except (UnicodeEncodeError, AttributeError):
        return False


def init_tesseract(app_dir):
    """
    Tesseract 5.x는 Windows에서 실행 경로 또는 TESSDATA_PREFIX에
    한글 등 비ASCII 문자가 포함되어 있으면 std::filesystem 예외
    ('Cannot convert character sequence: Illegal byte sequence')를 발생시킵니다.
    따라서 실행 경로에 한글이 포함된 경우, 모든 Windows에서 보장되는 안전한 영문 전용 디렉토리
    (C:\ProgramData\BD2Controller\Tesseract-OCR)로 엔진 및 언어팩을 미러링하여 사용합니다.
    """
    local_tess_dir = os.path.join(app_dir, "Tesseract-OCR")
    local_tess_exe = os.path.join(local_tess_dir, "tesseract.exe")
    local_tessdata = os.path.join(local_tess_dir, "tessdata")

    # 1. 실행 경로가 순수 영문(ASCII)이고 로컬에 tesseract.exe가 있는 경우 그대로 사용
    if is_ascii_str(app_dir) and os.path.exists(local_tess_exe):
        pytesseract.pytesseract.tesseract_cmd = local_tess_exe
        os.environ["TESSDATA_PREFIX"] = local_tessdata
        return local_tess_exe, local_tessdata

    # 2. 한글/특수문자가 포함된 경로인 경우: 영문 전용 안전 디렉토리 탐색
    candidate_roots = [
        os.environ.get("ProgramData", "C:\\ProgramData"),
        os.environ.get("PUBLIC", "C:\\Users\\Public"),
    ]
    safe_root = None
    for cand in candidate_roots:
        if cand and is_ascii_str(cand) and os.path.exists(cand):
            safe_root = cand
            break

    if not safe_root:
        safe_root = "C:\\ProgramData"

    safe_tess_dir = os.path.join(safe_root, "BD2Controller", "Tesseract-OCR")
    safe_tessdata = os.path.join(safe_tess_dir, "tessdata")
    safe_tess_exe = os.path.join(safe_tess_dir, "tesseract.exe")

    try:
        os.makedirs(safe_tessdata, exist_ok=True)
        if os.path.exists(local_tess_dir):
            for item in os.listdir(local_tess_dir):
                s = os.path.join(local_tess_dir, item)
                d = os.path.join(safe_tess_dir, item)
                if os.path.isdir(s):
                    if not os.path.exists(d):
                        shutil.copytree(s, d)
                else:
                    if not os.path.exists(d) or os.path.getsize(s) != os.path.getsize(
                        d
                    ):
                        shutil.copy2(s, d)

        if os.path.exists(safe_tess_exe):
            pytesseract.pytesseract.tesseract_cmd = safe_tess_exe
            os.environ["TESSDATA_PREFIX"] = safe_tessdata
            return safe_tess_exe, safe_tessdata
    except Exception as e:
        safe_print(f"[경고] Tesseract 안전 경로 복사 실패: {e}")

    # Fallback
    pytesseract.pytesseract.tesseract_cmd = local_tess_exe
    os.environ["TESSDATA_PREFIX"] = local_tessdata
    return local_tess_exe, local_tessdata


# Tesseract 안전 초기화 실행
TESS_EXE_PATH, TESS_DATA_PATH = init_tesseract(base_dir)

adb_path = os.path.join(base_dir, "adb.exe")
ADB_EXEC = adb_path if os.path.exists(adb_path) else "adb"
ADB_BIN = f'"{adb_path}"' if os.path.exists(adb_path) else "adb"
if base_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = base_dir + os.pathsep + os.environ.get("PATH", "")


def run_adb_cmd(args, timeout=10, check=False):
    """
    ADB 명령어를 cmd.exe(shell=True)를 거치지 않고 직접 리스트로 실행하여
    따옴표 깨짐 및 한글/공백 경로 문제를 원천 방지합니다.
    """
    cmd = [ADB_EXEC] + [str(a) for a in args]
    return subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=check,
    )


OCR_TEST_LOG_PATH = os.path.join(base_dir, "ocr_test.log")

is_macro_running = False
macro_thread = None


# ==============================================================================
# 2. 데이터베이스 및 파일 I/O 유틸리티
# ==============================================================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_config(filepath=CONFIG_PATH):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def save_config_with_backup(data):
    if os.path.exists(CONFIG_PATH):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"config_{timestamp}.yaml"
        backup_path = os.path.join(BACKUP_DIR, backup_filename)
        shutil.copy(CONFIG_PATH, backup_path)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(
            data, f, allow_unicode=True, default_flow_style=False, sort_keys=False
        )


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def safe_print(text):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("cp949", errors="replace").decode("cp949"))
    except Exception:
        pass


def debug_log(msg):
    """설정에서 디버그 모드가 켜져 있을 때만 서버 콘솔 및 웹 UI 로그에 출력"""
    config = load_config()
    is_debug = config.get("options", {}).get("debug_mode", False)

    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    formatted_msg = f"[{timestamp}] {msg}"

    # 디버그 모드가 켜져 있을 때만 서버 콘솔과 웹 UI 로그창에 모두 출력/적재
    if is_debug:
        safe_print(f"[{timestamp}] [DEBUG] {msg}")
        if len(web_logs_queue) > 100:
            web_logs_queue.pop(0)
        web_logs_queue.append(formatted_msg)


def macro_log(msg):
    """디버그 모드와 상관없이 항상 서버 콘솔과 웹 UI 로그창에 출력되는 일반 매크로 로그"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"

    # 서버 콘솔에 기본 출력
    safe_print(f"[{timestamp}] [INFO] {msg}")

    # 웹 UI 로그창 큐에 항상 적재
    if len(web_logs_queue) > 100:
        web_logs_queue.pop(0)
    web_logs_queue.append(formatted_msg)


def log_ocr_test(loop_count, slot_num, matched_name, ocr_data, is_match=None, note=""):
    """5성 코스튬 OCR 테스트 결과를 ocr_test.log 파일에 UTF-8로 기록"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"[{timestamp}] [{loop_count}회차 / 슬롯 {slot_num}] 5성 코스튬 OCR 테스트"

    char_raw = ocr_data.get("char_raw", "")
    char_filtered = ocr_data.get("raw_char_name", "")
    char_corrected = ocr_data.get("char_name", "")

    costume_raw = ocr_data.get("costume_raw", "")
    costume_filtered = ocr_data.get("raw_costume_name", "")
    costume_corrected = ocr_data.get("costume_name", "")

    full_name = ocr_data.get("full_costume_name", "")

    lines = [
        "=" * 70,
        header,
        f"  - 이미지 매칭 결과 : {matched_name if matched_name else '(매칭 없음 - 신규)'}",
        f"  - 캐릭터명 OCR   : Raw='{char_raw}' -> Filtered='{char_filtered}' -> Corrected='{char_corrected}'",
        f"  - 코스튬명 OCR   : Raw='{costume_raw}' -> Filtered='{costume_filtered}' -> Corrected='{costume_corrected}'",
        f"  - 최종 OCR 판독  : {full_name if full_name else '(인식 실패)'}",
    ]
    if is_match is True:
        lines.append(
            f"  - 검증 결과      : [일치 (MATCH)] 이미지 매칭명과 OCR 판독명이 일치합니다."
        )
    elif is_match is False:
        lines.append(
            f"  - 검증 결과      : [불일치 (MISMATCH)] 매칭='{matched_name}' vs OCR='{full_name}'"
        )
    else:
        status_str = note or "테스트 완료"
        lines.append(f"  - 검증 결과      : [{status_str}]")
    lines.append("=" * 70 + "\n")

    log_text = "\n".join(lines)
    try:
        with open(OCR_TEST_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(log_text)
    except Exception as e:
        debug_log(f"ocr_test.log 기록 실패: {e}")


def send_discord_notification(
    config, content="", embed_title="", embed_desc="", image_path=None, wait=False
):
    """디스코드 봇을 통해 채널로 메시지 및 스크린샷 전송"""
    discord_conf = config.get("discord", {})
    if not discord_conf.get("enabled", False):
        return False, "디스코드 연동이 비활성화되어 있습니다."

    bot_token = (discord_conf.get("bot_token") or "").strip()
    channel_id = (discord_conf.get("channel_id") or "").strip()
    user_id = (discord_conf.get("user_id") or "").strip()

    if not bot_token or not channel_id or bot_token == "YOUR_DISCORD_BOT_TOKEN_HERE":
        msg = "봇 토큰 또는 채널 ID가 올바르지 않습니다."
        debug_log(f"디스코드 전송 실패: {msg}")
        return False, msg

    def _execute():
        mention_prefix = f"<@{user_id}> " if user_id else ""
        full_content = (
            f"{mention_prefix}{content}".strip() if (mention_prefix or content) else ""
        )

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {bot_token}"}

        payload = {}
        if full_content:
            payload["content"] = full_content

        embed = {}
        if embed_title:
            embed["title"] = embed_title
        if embed_desc:
            embed["description"] = embed_desc

        filename = None
        if image_path and os.path.exists(image_path):
            filename = os.path.basename(image_path)
            embed["image"] = {"url": f"attachment://{filename}"}

        if embed:
            embed["color"] = 0xBB86FC  # 보라색 테마
            payload["embeds"] = [embed]

        try:
            if filename and image_path and os.path.exists(image_path):
                with open(image_path, "rb") as f:
                    files = {"files[0]": (filename, f.read(), "image/png")}
                    data = {"payload_json": json.dumps(payload)}
                    res = requests.post(
                        url, headers=headers, data=data, files=files, timeout=15
                    )
            else:
                res = requests.post(url, headers=headers, json=payload, timeout=15)

            if res.status_code in (200, 201):
                macro_log("📨 [디스코드] 알림 메시지 전송 성공!")
                try:
                    return True, res.json()
                except Exception:
                    return True, "전송 성공"
            else:
                err_text = res.text
                macro_log(
                    f"❌ [디스코드 전송 실패 (코드 {res.status_code})]: {err_text}"
                )
                return False, f"HTTP {res.status_code}: {err_text}"
        except Exception as e:
            macro_log(f"❌ [디스코드 전송 예외]: {e}")
            return False, str(e)

    if wait:
        return _execute()
    else:
        threading.Thread(target=_execute, daemon=True).start()
        return True, "전송 요청됨 (백그라운드)"


def add_discord_reactions(bot_token, channel_id, message_id):
    """결과 메시지에 🔄(재시도)와 ⏹️(종료) 이모지 리액션 자동 추가"""
    headers = {"Authorization": f"Bot {bot_token}"}
    for emoji in ["%F0%9F%94%84", "%E2%8F%B9"]:
        try:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me"
            requests.put(url, headers=headers, timeout=5)
            time.sleep(0.3)
        except Exception as e:
            debug_log(f"디스코드 이모지 리액션 추가 실패: {e}")


def wait_for_discord_decision(config, target_message_id, timeout_sec=300):
    """
    디스코드 채널에서 사용자의 '재시도' 또는 '종료' 선택을 대기
    반환값: 'retry', 'stop', 'timeout'
    """
    global is_macro_running
    discord_conf = config.get("discord", {})
    bot_token = (discord_conf.get("bot_token") or "").strip()
    channel_id = (discord_conf.get("channel_id") or "").strip()

    if not bot_token or not channel_id or not target_message_id:
        return "stop"

    headers = {"Authorization": f"Bot {bot_token}"}
    start_time = time.time()
    macro_log(
        f"⏳ [디스코드] 사용자 선택 대기 중... (채팅에 '재시도' 또는 '종료' 입력, 최대 {timeout_sec // 60}분)"
    )

    retry_keywords = ["재시도", "다시", "1", "retry", "r", "ㄱ"]
    stop_keywords = ["종료", "끝", "스톱", "2", "stop", "s", "q"]

    while time.time() - start_time < timeout_sec:
        # 1. WebUI에서 중지 버튼을 누른 경우 즉시 종료
        if not is_macro_running:
            return "stop"

        try:
            # 2. 채팅 메시지 확인 (해당 결과 메시지 이후로 도착한 메시지 조회)
            msg_url = f"https://discord.com/api/v10/channels/{channel_id}/messages?after={target_message_id}&limit=10"
            res = requests.get(msg_url, headers=headers, timeout=5)
            if res.status_code == 200:
                messages = res.json()
                for msg in messages:
                    # 봇 자신이 보낸 메시지는 제외
                    if msg.get("author", {}).get("bot"):
                        continue
                    text = msg.get("content", "").strip().lower()
                    for k in retry_keywords:
                        if text == k or text.startswith(k):
                            macro_log(
                                f"💬 [디스코드 응답 감지] '{text}' -> '재시도' 선택됨"
                            )
                            return "retry"
                    for k in stop_keywords:
                        if text == k or text.startswith(k):
                            macro_log(
                                f"💬 [디스코드 응답 감지] '{text}' -> '종료' 선택됨"
                            )
                            return "stop"

            # 3. 이모지 리액션 클릭 확인
            for emoji_code, decision in [
                ("%F0%9F%94%84", "retry"),
                ("%E2%8F%B9", "stop"),
            ]:
                r_url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{target_message_id}/reactions/{emoji_code}?limit=10"
                r_res = requests.get(r_url, headers=headers, timeout=5)
                if r_res.status_code == 200:
                    users = r_res.json()
                    # 봇 본인이 아닌 실제 사용자가 누른 경우
                    human_users = [u for u in users if not u.get("bot")]
                    if human_users:
                        macro_log(
                            f"👍 [디스코드 리액션 감지] {decision.upper()} 선택됨"
                        )
                        return decision

        except Exception as poll_err:
            debug_log(f"디스코드 폴링 오류: {poll_err}")

        time.sleep(2.0)

    macro_log("⏰ [디스코드] 대기 시간(5분) 초과로 매크로를 안전하게 자동 종료합니다.")
    return "timeout"


# ==============================================================================
# 3. 이미지 처리, OCR 및 명칭 매칭 로직
# ==============================================================================
def preprocess_for_ocr(crop_img):
    if crop_img is None or crop_img.size == 0:
        return crop_img
    resized = cv2.resize(crop_img, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    _, binary = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    padded = cv2.copyMakeBorder(binary, 40, 40, 40, 40, cv2.BORDER_REPLICATE)
    return padded


def extract_text_from_image(cv_img, lang="kor+eng", log_prefix=""):
    try:
        if not os.path.exists(pytesseract.pytesseract.tesseract_cmd):
            raise FileNotFoundError("Tesseract 엔진 경로를 찾을 수 없습니다.")
        if len(cv_img.shape) == 3:
            cv_img = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
        elif len(cv_img.shape) == 4:
            cv_img = cv2.cvtColor(cv_img, cv2.COLOR_BGRA2RGB)

        pil_img = Image.fromarray(cv_img)
        custom_config = "--psm 7 -c preserve_interword_spaces=1"
        text = pytesseract.image_to_string(pil_img, lang=lang, config=custom_config)

        filtered_text = re.sub(r"[^가-힣a-zA-Z0-9\s]", "", text).strip()
        debug_log(
            f"{log_prefix} OCR Raw: '{text.strip()}' -> Filtered: '{filtered_text}'"
        )
        return filtered_text
    except Exception as e:
        debug_log(f"OCR 인식 오류: {e}")
        return ""


def clean_korean_text(text):
    if not text:
        return ""
    cleaned = re.sub(r"[^가-힣a-zA-Z0-9\s]", "", text)
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    return re.sub(r"_+", "_", cleaned)


def apply_char_correction(text):
    if not text:
        return ""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT correct_text FROM char_corrections WHERE wrong_text = ?", (text,)
    ).fetchone()
    conn.close()
    if row:
        debug_log(f"캐릭터명 DB 보정 적용: {text} -> {row['correct_text']}")
    return row["correct_text"] if row else text


def apply_costume_correction(text):
    if not text:
        return ""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT correct_text FROM costume_corrections WHERE wrong_text = ?", (text,)
    ).fetchone()
    conn.close()
    if row:
        debug_log(f"코스튬명 DB 보정 적용: {text} -> {row['correct_text']}")
    return row["correct_text"] if row else text


def get_all_costumes_from_db():
    conn = get_db_connection()
    costumes = [dict(row) for row in conn.execute("SELECT * FROM costumes").fetchall()]
    conn.close()
    return costumes


_TEMPLATE_CACHE = {}


def clear_template_cache():
    """템플릿 이미지 캐시 초기화"""
    global _TEMPLATE_CACHE
    _TEMPLATE_CACHE.clear()


def get_template_image(img_path, target_shape=None):
    """
    템플릿 이미지를 메모리에 캐싱하여 디스크 I/O 반복을 방지합니다.
    target_shape: (height, width)
    """
    global _TEMPLATE_CACHE
    if not img_path:
        return None
    if not os.path.isabs(img_path):
        img_path = os.path.join(base_dir, img_path)
    if not os.path.exists(img_path):
        return None

    cache_key = (img_path, target_shape)
    if cache_key in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[cache_key]

    template = imread_utf8(img_path, cv2.IMREAD_COLOR)
    if template is not None and target_shape is not None:
        if template.shape[:2] != target_shape:
            template = cv2.resize(template, (target_shape[1], target_shape[0]))

    if template is not None:
        _TEMPLATE_CACHE[cache_key] = template

    return template


def match_costume_from_db(card_crop, db_costumes, threshold=0.80):
    if card_crop is None or card_crop.size == 0:
        return None
    if card_crop.shape[0] < 10 or card_crop.shape[1] < 10:
        return None

    if len(card_crop.shape) == 3 and card_crop.shape[2] == 4:
        card_crop = cv2.cvtColor(card_crop, cv2.COLOR_BGRA2BGR)

    # 1. 단색 / 암전 / 로딩 화면 등 유효하지 않은 슬롯 예외 처리 (표준편차 체크)
    crop_std = float(np.std(card_crop))
    if crop_std < 10.0:
        debug_log(
            f"슬롯 이미지 분산 부족 (std: {crop_std:.2f} < 10.0) - 빈 화면 또는 암전으로 판정하여 매칭 제외"
        )
        return None

    target_shape = card_crop.shape[:2]
    best_match_name = None
    best_max_val = -1.0

    for costume in db_costumes:
        img_path = costume.get("image_filename") or costume.get("image_path")
        template = get_template_image(img_path, target_shape=target_shape)
        if template is None or float(np.std(template)) < 10.0:
            continue

        res = cv2.matchTemplate(card_crop, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(res)

        if max_val > best_max_val:
            best_max_val = max_val
            best_match_name = costume.get("costume_name", "")

    # 전체 DB 탐색 후, 최고 유사도가 임계값 이상인 경우에만 최종 매칭 인정 (Best-Match 방식)
    if best_max_val >= threshold and best_match_name:
        debug_log(
            f"템플릿 매칭 성공: {best_match_name} (최고 Score: {best_max_val:.4f} >= {threshold})"
        )
        return best_match_name

    if best_match_name:
        debug_log(
            f"템플릿 매칭 실패. 최고 유사도: {best_match_name} (Score: {best_max_val:.4f} < 임계값 {threshold})"
        )
    return None


def format_slot_display(slot_num, raw_costume_name, rarity=5, is_new=False):
    """
    DB의 '캐릭터명_코스튬명' 및 성급을 읽기 좋은 깔끔한 형식으로 변환합니다.
    언더스코어(_)를 공백 및 대시(-)로 변환하여 디스코드 마크다운(기울임꼴) 충돌을 방지합니다.
    예:
      '레클리스_안드로이드_퀸', 5 -> '[슬롯 1] ⭐ [5성] 레클리스 - 안드로이드 퀸'
      '베르니_의적_소녀', 4        -> '[슬롯 8] 🔷 [4성] 베르니 - 의적 소녀'
      '위글_폭탄광', 3             -> '[슬롯 3] ▫️ [3성] 위글 - 폭탄광'
    """
    if not raw_costume_name:
        return f"[슬롯 {slot_num}] ❓ 미인식"

    parts = raw_costume_name.split("_", 1)
    char_name = parts[0].strip()
    costume_part = parts[1].replace("_", " ").strip() if len(parts) > 1 else ""

    display_name = f"{char_name} - {costume_part}" if costume_part else char_name
    new_tag = " (신규)" if is_new else ""

    if rarity == 5:
        icon = "⭐"
    elif rarity == 4:
        icon = "🔷"
    else:
        icon = "▫️"

    return f"[슬롯 {slot_num}] {icon} [{rarity}성] {display_name}{new_tag}"


def imread_utf8(filepath, flags=cv2.IMREAD_COLOR):
    try:
        nparr = np.fromfile(filepath, np.uint8)
        return cv2.imdecode(nparr, flags)
    except Exception as e:
        debug_log(f"[imread_utf8 에러] {filepath} 로드 실패: {e}")
        return None


def save_cv2_image_utf8(filepath, img):
    try:
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
        ext = os.path.splitext(filepath)[1]
        result, nparr = cv2.imencode(ext, img)
        if result:
            with open(filepath, "wb") as f:
                nparr.tofile(f)
            return True
        return False
    except Exception as e:
        debug_log(f"[이미지 저장 에러] {filepath} 저장 실패: {e}")
        return False


# ==============================================================================
# 4. ADB 제어 로직
# ==============================================================================
def get_target_device_id(config=None):
    """설정된 adb_device가 있으면 해당 ID(시리얼 또는 IP:포트)를, 없으면 기본 IP:포트를 반환"""
    if config is None:
        config = load_config()
    adb_dev = (config.get("adb_device") or "").strip()
    if adb_dev:
        return adb_dev
    return f"127.0.0.1:{config.get('adb_port', 5555)}"


def get_connected_devices():
    """adb devices -l 출력을 파싱하여 연결된 기기 목록 반환"""
    try:
        res = run_adb_cmd(["devices", "-l"], timeout=5)
        lines = (res.stdout or "").splitlines()
        devices = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                dev_id = parts[0]
                state = parts[1].lower()
                dev_info = {
                    "id": dev_id,
                    "state": state,
                    "model": "",
                    "product": "",
                    "type": (
                        "network"
                        if (":" in dev_id or dev_id.startswith("127.0.0.1"))
                        else "usb"
                    ),
                }
                for p in parts[2:]:
                    if ":" in p:
                        k, v = p.split(":", 1)
                        if k == "model":
                            dev_info["model"] = v
                        elif k == "product":
                            dev_info["product"] = v

                if not dev_info["model"]:
                    dev_info["model"] = dev_id

                devices.append(dev_info)
        return devices
    except Exception as e:
        debug_log(f"기기 목록 조회 실패: {e}")
        return []


def check_and_connect_adb(device_id):
    """
    ADB 장치 연결을 시도하고 상태(device)를 검증합니다.
    - IP:포트 형태 (네트워크/에뮬레이터)인 경우: adb connect 시도
    - USB 시리얼 형태인 경우: adb connect를 부르지 않고 adb devices 목록 확인
    성공 시 (True, '성공 메시지'), 실패 시 (False, '실패 원인') 반환
    """
    try:
        is_network = ":" in device_id or device_id.startswith("127.0.0.1")
        if is_network:
            debug_log(f"네트워크 ADB 연결 시도: {device_id}")
            run_adb_cmd(["connect", device_id], timeout=10)
        else:
            debug_log(f"USB/시리얼 기기 상태 확인: {device_id}")

        devices_result = run_adb_cmd(["devices", "-l"], timeout=5)
        stdout = devices_result.stdout or ""

        for line in stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == device_id:
                state = parts[1].lower()
                if state == "device":
                    return True, f"ADB 장치 ({device_id}) 정상 연결됨"
                elif state == "unauthorized":
                    return (
                        False,
                        f"기기 승인 필요: 휴대전화 화면에서 'USB 디버깅을 항상 허용'을 눌러주세요.",
                    )
                elif state == "offline":
                    return (
                        False,
                        f"기기가 오프라인 상태입니다. USB 케이블 연결을 확인하세요.",
                    )
                else:
                    return False, f"ADB 장치 ({device_id}) 상태 비정상 ({state})"

        return (
            False,
            f"ADB 장치 목록에서 '{device_id}'를 찾을 수 없습니다. 케이블 연결 및 USB 디버깅 설정을 확인하세요.",
        )
    except subprocess.TimeoutExpired:
        return False, f"ADB 응답 시간 초과 ({device_id})"
    except Exception as e:
        return False, f"ADB 연결 중 오류 발생: {e}"


def get_device_resolution(device_id):
    """선택된 기기의 물리 해상도 및 현재 적용된 해상도(Override size) 조회"""
    try:
        res = run_adb_cmd(["-s", device_id, "shell", "wm", "size"], timeout=5)
        stdout = res.stdout or ""
        physical = ""
        override = ""
        for line in stdout.splitlines():
            line = line.strip()
            if "Physical size:" in line:
                physical = line.replace("Physical size:", "").strip()
            elif "Override size:" in line:
                override = line.replace("Override size:", "").strip()
        return physical, override
    except Exception as e:
        debug_log(f"해상도 조회 오류: {e}")
        return "", ""


def is_emulator_device(device_id):
    """지정된 ADB 장치가 앱플레이어(에뮬레이터)인지 판별"""
    if not device_id:
        return False
    dev_str = device_id.lower()
    # 1. IP:포트, localhost, emulator- 접두사 확인
    if any(dev_str.startswith(p) for p in ["127.0.0.1:", "localhost:", "emulator-"]):
        return True

    # 2. get_connected_devices() 목록 확인
    try:
        devices = get_connected_devices()
        for d in devices:
            if d.get("id") == device_id:
                m = (d.get("model") or "").lower()
                p = (d.get("product") or "").lower()
                t = d.get("type", "")
                if t == "network" or any(
                    emu in m or emu in p
                    for emu in [
                        "emulator",
                        "nox",
                        "bluestacks",
                        "ldplayer",
                        "mumu",
                        "vbox",
                        "bignox",
                    ]
                ):
                    return True
    except Exception:
        pass

    # 3. getprop 질의
    try:
        res = run_adb_cmd(
            ["-s", device_id, "shell", "getprop", "ro.product.model"], timeout=2
        )
        model = (res.stdout or "").strip().lower()
        if any(
            emu in model
            for emu in ["emulator", "nox", "bluestacks", "ldplayer", "mumu", "vbox"]
        ):
            return True
        res_hw = run_adb_cmd(
            ["-s", device_id, "shell", "getprop", "ro.hardware"], timeout=2
        )
        hw = (res_hw.stdout or "").strip().lower()
        if any(emu in hw for emu in ["vbox", "nox", "ttvm", "goldfish", "ranchu"]):
            return True
    except Exception:
        pass

    return False


def get_configured_resolution():
    """현재 config.yaml에 설정된 해상도 정보(width, height, name) 반환"""
    config = load_config()
    selected_name = config.get("resolution")
    if not selected_name:
        return 1920, 1080, "FHD (1920x1080 - 280DPI)"
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT width, height, name FROM resolutions WHERE name = ?",
            (selected_name,),
        ).fetchone()
        conn.close()
        if row:
            return int(row["width"]), int(row["height"]), row["name"]
    except Exception:
        pass
    return 1920, 1080, selected_name


def check_resolution_match(device_id):
    """
    현재 연결된 기기/앱플레이어의 해상도가 설정된 해상도와 일치하는지 종합 검증
    """
    target_w, target_h, target_name = get_configured_resolution()
    is_emu = is_emulator_device(device_id)

    physical, override = get_device_resolution(device_id)
    cur_res_str = override if override else physical

    if not cur_res_str or "x" not in cur_res_str:
        return {
            "matched": False,
            "device_res": "미확인",
            "physical_size": physical,
            "override_size": override,
            "target_res": f"{target_w}x{target_h}",
            "is_emulator": is_emu,
            "is_portrait": False,
            "message": "기기 해상도를 조회할 수 없습니다.",
        }

    try:
        parts = cur_res_str.lower().split("x")
        cur_w, cur_h = int(parts[0].strip()), int(parts[1].strip())
    except Exception:
        return {
            "matched": False,
            "device_res": cur_res_str,
            "physical_size": physical,
            "override_size": override,
            "target_res": f"{target_w}x{target_h}",
            "is_emulator": is_emu,
            "is_portrait": False,
            "message": f"해상도 파싱 오류 ({cur_res_str})",
        }

    # 1920x1080 또는 1080x1920 등 가로/세로 매칭 검사
    matched = sorted([cur_w, cur_h]) == sorted([target_w, target_h])
    is_portrait = cur_w < cur_h

    if matched:
        if is_portrait:
            msg = f"해상도 규격 일치 ({cur_w}x{cur_h} ↔ {target_w}x{target_h}) ✅ (세로 모드 감지)"
        else:
            msg = f"해상도 일치 ({cur_w}x{cur_h}) ✅"
    else:
        if is_emu:
            msg = f"앱플레이어 해상도 불일치: 현재 {cur_w}x{cur_h} (설정: {target_w}x{target_h}) ⚠️"
        else:
            msg = f"기기 해상도 불일치: 현재 {cur_w}x{cur_h} (설정: {target_w}x{target_h})"

    return {
        "matched": matched,
        "device_res": cur_res_str,
        "physical_size": physical,
        "override_size": override,
        "target_res": f"{target_w}x{target_h}",
        "is_emulator": is_emu,
        "is_portrait": is_portrait,
        "message": msg,
    }


def set_device_resolution(device_id, width, height, density=None):
    """기기 해상도 및 DPI 임시 변경 (선택사항)"""
    try:
        run_adb_cmd(
            ["-s", device_id, "shell", "wm", "size", f"{width}x{height}"],
            timeout=5,
        )
        if density:
            run_adb_cmd(
                ["-s", device_id, "shell", "wm", "density", str(density)],
                timeout=5,
            )
        return True
    except Exception as e:
        debug_log(f"해상도 변경 실패: {e}")
        return False


def reset_device_resolution(device_id):
    """기기 해상도 및 DPI 원래대로 복구"""
    try:
        run_adb_cmd(["-s", device_id, "shell", "wm", "size", "reset"], timeout=5)
        run_adb_cmd(["-s", device_id, "shell", "wm", "density", "reset"], timeout=5)
        return True
    except Exception as e:
        debug_log(f"해상도 복구 실패: {e}")
        return False


def capture_adb_screenshot(device_id):
    try:
        debug_log("ADB 스크린샷 캡처 요청 중...")
        run_adb_cmd(
            ["-s", device_id, "shell", "screencap", "-p", "/sdcard/screenshot.png"],
            timeout=10,
            check=True,
        )

        local_path = os.path.join(base_dir, "screenshot.png")
        # pull 시 list 인자로 실행하여 한글/공백 경로 및 따옴표 문제 방지
        run_adb_cmd(
            ["-s", device_id, "pull", "/sdcard/screenshot.png", local_path],
            timeout=10,
            check=True,
        )

        run_adb_cmd(
            ["-s", device_id, "shell", "rm", "/sdcard/screenshot.png"], timeout=5
        )

        return local_path if os.path.exists(local_path) else None
    except Exception as e:
        debug_log(f"ADB 스크린샷 오류: {e}")
        return None


def execute_adb_click(device_id, x, y, duration_ms=50):
    debug_log(f"ADB 터치 이벤트 전송 -> x:{int(x)}, y:{int(y)} ({duration_ms}ms)")
    if duration_ms and duration_ms > 0:
        run_adb_cmd(
            [
                "-s",
                device_id,
                "shell",
                "input",
                "swipe",
                str(int(x)),
                str(int(y)),
                str(int(x)),
                str(int(y)),
                str(int(duration_ms)),
            ],
            timeout=10,
        )
    else:
        run_adb_cmd(
            ["-s", device_id, "shell", "input", "tap", str(int(x)), str(int(y))],
            timeout=10,
        )


def perform_slot_ocr(
    device_id, center_x, center_y, char_roi, costume_roi, click_delay, slot_num=1
):
    """슬롯의 상세 정보창을 열고 스크린샷 캡쳐 후 캐릭터명/코스튬명 OCR 판독 및 보정을 수행하고 창을 닫음."""
    result = {
        "success": False,
        "char_raw": "",
        "raw_char_name": "",
        "char_name": "",
        "costume_raw": "",
        "raw_costume_name": "",
        "costume_name": "",
        "full_costume_name": "",
        "error_msg": "",
    }

    try:
        debug_log(f"[슬롯 {slot_num}] 상세 정보창 클릭 (x:{center_x}, y:{center_y})")
        execute_adb_click(device_id, center_x, center_y)
        time.sleep(max(click_delay, 1.0))

        detail_ss = capture_adb_screenshot(device_id)
        if not detail_ss or not os.path.exists(detail_ss):
            result["error_msg"] = "스크린샷 캡처 실패"
            return result

        detail_img = imread_utf8(detail_ss)
        if detail_img is None:
            result["error_msg"] = "상세 이미지 로드 실패"
            return result

        char_raw, costume_raw = "", ""

        if char_roi and char_roi.get("x") is not None:
            cx, cy, cw, ch = (
                int(char_roi["x"]),
                int(char_roi["y"]),
                int(char_roi["w"]),
                int(char_roi["h"]),
            )
            debug_log(
                f"[슬롯 {slot_num}] 캐릭터명 ROI 크롭 (x:{cx}, y:{cy}, w:{cw}, h:{ch})"
            )
            char_crop = detail_img[cy : cy + ch, cx : cx + cw]
            char_raw = extract_text_from_image(
                preprocess_for_ocr(char_crop),
                log_prefix=f"[슬롯 {slot_num} 캐릭터명]",
            )

        if costume_roi and costume_roi.get("x") is not None:
            kx, ky, kw, kh = (
                int(costume_roi["x"]),
                int(costume_roi["y"]),
                int(costume_roi["w"]),
                int(costume_roi["h"]),
            )
            debug_log(
                f"[슬롯 {slot_num}] 코스튬명 ROI 크롭 (x:{kx}, y:{ky}, w:{kw}, h:{kh})"
            )
            costume_crop = detail_img[ky : ky + kh, kx : kx + kw]
            costume_raw = extract_text_from_image(
                preprocess_for_ocr(costume_crop),
                log_prefix=f"[슬롯 {slot_num} 코스튬명]",
            )

        raw_char_name = clean_korean_text(char_raw)
        raw_costume_name = clean_korean_text(costume_raw)
        char_name = apply_char_correction(raw_char_name)
        costume_name = apply_costume_correction(raw_costume_name)

        result["char_raw"] = char_raw
        result["raw_char_name"] = raw_char_name
        result["char_name"] = char_name
        result["costume_raw"] = costume_raw
        result["raw_costume_name"] = raw_costume_name
        result["costume_name"] = costume_name

        if char_name and costume_name:
            result["full_costume_name"] = f"{char_name}_{costume_name}"
            result["success"] = True
        else:
            result["error_msg"] = (
                f"명칭 인식 부족 (캐릭터:{char_name or '미인식'}, 코스튬:{costume_name or '미인식'})"
            )

    except Exception as e:
        result["error_msg"] = f"OCR 처리 중 예외 발생: {e}"
        debug_log(f"[슬롯 {slot_num}] perform_slot_ocr 에러: {e}")
    finally:
        # 항상 상세창을 닫도록 보장
        try:
            execute_adb_click(device_id, 1, 1)
            time.sleep(click_delay)
        except Exception:
            pass

    return result


def is_retry_button_present(image, gacha_pos, template=None):
    """
    현재 화면에서 '다시 뽑기' 버튼이 출현했는지 여부를 신속하고 정확하게 판별합니다.
    브라운더스트2의 '다시 뽑기' 버튼 특유의 선명한 하늘색(Cyan/Sky-Blue) 색상 비율 및
    템플릿 매칭 점수를 종합하여 밀리초 단위로 정확하게 감지합니다.
    """
    if image is None or not gacha_pos:
        return False
    gx = gacha_pos.get("x")
    gy = gacha_pos.get("y")
    if gx is None or gy is None:
        return False
    gx, gy = int(gx), int(gy)
    h, w = image.shape[:2]
    if not (0 <= gx < w and 0 <= gy < h):
        return False

    # 기준 해상도(1920) 대비 가로폭 스케일 비율
    scale = w / 1920.0 if w > 0 else 1.0
    dx = int(min(150 * scale, gx, w - gx))
    dy = int(min(50 * scale, gy, h - gy))
    if dx < 10 or dy < 5:
        return False

    crop = image[gy - dy : gy + dy, gx - dx : gx + dx]
    if crop.size == 0:
        return False

    # 1. 하늘색(Cyan/Sky-Blue) 색상 비율 검사 (HSV: H=90~115, S=80~255, V=150~255)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    lower_blue = np.array([90, 80, 150])
    upper_blue = np.array([115, 255, 255])
    mask = cv2.inRange(hsv, lower_blue, upper_blue)
    blue_ratio = np.count_nonzero(mask) / mask.size

    # 2. 템플릿 매칭 검사
    tmpl_match = False
    if template is not None:
        try:
            th, tw = template.shape[:2]
            if scale != 1.0:
                scaled_tmpl = cv2.resize(
                    template,
                    (max(10, int(tw * scale)), max(5, int(th * scale))),
                )
            else:
                scaled_tmpl = template
            if (
                crop.shape[0] >= scaled_tmpl.shape[0]
                and crop.shape[1] >= scaled_tmpl.shape[1]
            ):
                res = cv2.matchTemplate(crop, scaled_tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(res)
                if max_val >= 0.70:
                    tmpl_match = True
        except Exception:
            pass

    # 템플릿이 로드된 경우: 템플릿 매칭(>= 0.70)과 하늘색 비율(>= 0.30)을 동시 만족해야만 인정 (오탐 원천 차단)
    if template is not None:
        return tmpl_match and (blue_ratio >= 0.30)

    # 템플릿이 없는 경우: 하늘색 비율 기준 판별
    return blue_ratio >= 0.40


def wait_for_retry_button_with_skip_tapping(
    device_id, skip_btn, gacha_btn, res_data, max_timeout=20.0
):
    """
    뽑기 진행 후 결과 화면('다시 뽑기' 버튼)이 나타날 때까지
    스킵 버튼을 0.1초 간격으로 연속 클릭하며 대기합니다.
    신규 코스튬 획득 안내 팝업 등 추가 스킵이 필요한 상황을 전자동으로 처리합니다.
    """
    global is_macro_running

    skip_x = skip_btn.get("x")
    skip_y = skip_btn.get("y")
    target_w = int(res_data.get("width", 1920))
    target_h = int(res_data.get("height", 1080))

    # 템플릿 이미지 로드
    retry_template_path = os.path.join(base_dir, "static", "retry_btn.png")
    retry_template = None
    if os.path.exists(retry_template_path):
        retry_template = imread_utf8(retry_template_path)

    stop_tapping = threading.Event()
    tap_count = [0]

    def _tap_worker():
        while not stop_tapping.is_set() and is_macro_running:
            if skip_x is not None and skip_y is not None:
                t_start = time.time()
                try:
                    # input tap(0ms) 대신 50ms 홀드 스와이프를 사용하여 유니티 게임 엔진의 터치 드랍(미인식) 원천 해결
                    run_adb_cmd(
                        [
                            "-s",
                            device_id,
                            "shell",
                            "input",
                            "swipe",
                            str(int(skip_x)),
                            str(int(skip_y)),
                            str(int(skip_x)),
                            str(int(skip_y)),
                            "50",
                        ],
                        timeout=3,
                    )
                    tap_count[0] += 1
                except Exception as e:
                    debug_log(f"스킵 탭 에러: {e}")

                elapsed_tap = time.time() - t_start
                remain = max(0.01, 0.1 - elapsed_tap)
                if stop_tapping.wait(remain):
                    break
            else:
                if stop_tapping.wait(0.1):
                    break

    tap_thread = None
    if skip_x is not None and skip_y is not None:
        macro_log(
            f"⏩ [스킵 연타 시작] 결과 화면이 나올 때까지 스킵 버튼(x:{skip_x}, y:{skip_y})을 0.1초마다 반복 클릭합니다..."
        )
        tap_thread = threading.Thread(target=_tap_worker, daemon=True)
        tap_thread.start()

    start_time = time.time()
    last_screenshot_file = None
    last_src_img = None
    detected = False

    # 1단계: 가챠 시작 후 화면 전환 최소 대기 (2.0초)
    # 서버 응답 및 컷신 시작 전 이전 화면의 '다시 뽑기' 버튼으로 인한 조기 종료를 원천 방지
    # 이 2초 동안에도 백그라운드 스레드는 스킵 버튼을 0.1초마다 연타(~20회)합니다.
    min_anim_wait = 2.0
    while is_macro_running and (time.time() - start_time < min_anim_wait):
        time.sleep(0.5)
        if tap_count[0] > 0 and tap_count[0] % 10 == 0:
            macro_log(
                f"⏩ [스킵 연타 중] 가챠 연출 스킵 진행 중... ({tap_count[0]}회 클릭)"
            )

    # 2단계: 결과 화면('다시 뽑기' 버튼) 출현 대기
    try:
        last_log_tap = tap_count[0]
        while is_macro_running and (time.time() - start_time < max_timeout):
            ss_file = capture_adb_screenshot(device_id)
            if not ss_file or not os.path.exists(ss_file):
                time.sleep(0.2)
                continue

            img = imread_utf8(ss_file)
            if img is None:
                time.sleep(0.2)
                continue

            last_screenshot_file = ss_file

            # 화면 암전(로딩 중)인 경우 잠시 대기
            if float(np.mean(img)) < 15.0:
                time.sleep(0.3)
                continue

            # 해상도 보정
            if img.shape[1] != target_w or img.shape[0] != target_h:
                eval_img = cv2.resize(img, (target_w, target_h))
            else:
                eval_img = img

            last_src_img = eval_img

            # '다시 뽑기' 버튼 출현 여부 판별
            if is_retry_button_present(eval_img, gacha_btn, retry_template):
                detected = True
                break

            # 10회 이상 추가 탭 될 때마다 진행 상황 로그 출력
            if tap_count[0] - last_log_tap >= 10:
                macro_log(
                    f"⏩ [스킵 연타 중] 신규 코스튬 팝업/연출 스킵 대기 중... ({tap_count[0]}회 클릭)"
                )
                last_log_tap = tap_count[0]

            time.sleep(0.2)

    finally:
        # 결과 화면 감지 완료 또는 타임아웃/중단 시 스킵 터치 스레드 즉시 종료
        stop_tapping.set()
        if tap_thread and tap_thread.is_alive():
            tap_thread.join(timeout=1.0)

    elapsed = time.time() - start_time
    if detected:
        macro_log(
            f"✅ [결과 화면 감지 완료] '다시 뽑기' 버튼 출현 확인 (총 스킵 연타 {tap_count[0]}회, {elapsed:.1f}초)"
        )
    else:
        macro_log(
            f"⚠️ [결과 화면 감지 시간 초과] {max_timeout}초 동안 '다시 뽑기' 버튼이 감지되지 않아 현재 화면으로 계속 진행합니다."
        )

    return detected, last_screenshot_file, last_src_img


# ==============================================================================
# 5. 매크로 메인 루프
# ==============================================================================
def run_gacha_macro_loop():
    global is_macro_running
    macro_log("🚀 [매크로] 루프 프로세스가 백그라운드에서 시작되었습니다.")

    config = load_config()
    device_id = get_target_device_id(config)

    macro_log(f"🔌 [ADB 연결 확인] {device_id} 연결을 시도하고 상태를 확인합니다...")
    connected, msg = check_and_connect_adb(device_id)
    if not connected:
        macro_log(f"❌ [ADB 연결 실패] {msg}")
        macro_log(
            "[매크로 중단] ADB 연결에 실패하여 매크로를 시작할 수 없습니다. 기기 연결 및 USB 디버깅/포트 설정을 확인해 주세요."
        )
        is_macro_running = False
        return

    macro_log(f"✅ [ADB 연결 성공] {device_id} 정상 연결 확인됨.")

    # 해상도 일치 여부 검증
    res_check = check_resolution_match(device_id)
    target_w, target_h, target_name = get_configured_resolution()

    if res_check["is_emulator"]:
        if res_check["matched"]:
            macro_log(
                f"🖥️ [앱플레이어 감지] 해상도 일치 확인됨: {res_check['device_res']} == {target_w}x{target_h} ✅"
            )
        else:
            macro_log(
                f"⚠️ [앱플레이어 해상도 불일치] 현재 앱플레이어 해상도({res_check['device_res']})가 매크로 설정({target_w}x{target_h})과 다릅니다!"
            )
            macro_log(
                "💡 [권장] 원활한 카드 인식을 위해 앱플레이어 설정에서 해상도를 1920x1080 (16:9 가로 모드)으로 설정해주세요."
            )
    else:
        # 스마트폰 / 모바일 기기
        if res_check["matched"]:
            macro_log(
                f"📱 [모바일 기기] 해상도 일치 확인됨: {res_check['device_res']} ✅"
            )
        else:
            macro_log(
                f"📱 [모바일 기기 감지] 현재 기기 해상도: {res_check['device_res']} (설정: {target_w}x{target_h})"
            )
            if not options.get("auto_fhd_resolution", False):
                macro_log(
                    "💡 화면 비율(20:9 등)로 인해 인식이 어긋날 경우, 상단 '16:9 FHD 맞춤' 도구를 이용해주세요."
                )

    # 선택적 해상도 자동 맞춤 (기본값: False)
    options = config.get("options", {})
    auto_fhd_applied = False
    if options.get("auto_fhd_resolution", False):
        p_size, _ = get_device_resolution(device_id)
        if p_size and p_size not in ["1080x1920", "1920x1080"]:
            macro_log(
                f"📱 [해상도 자동 맞춤] 16:9 FHD(1080x1920)로 임시 변경합니다. (기기 원래 해상도: {p_size})"
            )
            if set_device_resolution(device_id, 1080, 1920, 400):
                auto_fhd_applied = True

    loop_count = 0

    while is_macro_running:
        try:
            print("\n" + "=" * 50)
            print("[매크로] 새로운 가챠 회차 시작")
            print("=" * 50)
            loop_count += 1

            config = load_config()
            selected_res = config.get("resolution")
            click_delay = float(config.get("click_delay", 0.5))
            device_id = get_target_device_id(config)

            options = config.get("options", {})
            # config.yaml에서 공통 인식률 불러오기
            global_threshold = float(options.get("match_threshold", 0.80))
            auto_add_new = options.get("auto_add_new_costume", True)
            ocr_test_5star = options.get("ocr_test_5star", False)

            if not selected_res:
                macro_log("[매크로 에러] config.yaml 해상도 설정 누락으로 중지합니다.")
                break

            conn = get_db_connection()
            res_info = conn.execute(
                "SELECT * FROM resolutions WHERE name = ?", (selected_res,)
            ).fetchone()
            conn.close()

            if not res_info:
                macro_log(
                    f"[매크로 에러] DB 해상도 설정({selected_res}) 누락으로 중지합니다."
                )
                break

            res_data = dict(res_info)
            gacha_btn = json.loads(res_data.get("gacha_btn_pos") or "{}")
            skip_btn = json.loads(res_data.get("skip_btn_pos") or "{}")
            confirm_btn = json.loads(res_data.get("confirm_btn_pos") or "{}")

            debug_log("버튼 순차 클릭 시작")
            if gacha_btn.get("x") and gacha_btn.get("y"):
                execute_adb_click(device_id, gacha_btn["x"], gacha_btn["y"])
                time.sleep(click_delay)

            if confirm_btn.get("x") and confirm_btn.get("y"):
                execute_adb_click(device_id, confirm_btn["x"], confirm_btn["y"])
                time.sleep(click_delay)

            # 신규 코스튬 안내 팝업 등 추가 스킵 대응: '다시 뽑기' 버튼이 나타날 때까지 스킵 버튼을 0.1초마다 반복 클릭
            macro_log(
                "⏩ [스킵 진행] 결과 화면('다시 뽑기' 버튼)이 나타날 때까지 스킵 버튼을 연속 클릭합니다..."
            )
            detected, screenshot_file, src_img = (
                wait_for_retry_button_with_skip_tapping(
                    device_id, skip_btn, gacha_btn, res_data, max_timeout=15.0
                )
            )

            if not is_macro_running:
                break

            if (
                not screenshot_file
                or not os.path.exists(screenshot_file)
                or src_img is None
            ):
                macro_log("[매크로 에러] ADB 스크린샷 실패 - 연결 상태 재확인 중...")
                connected, re_msg = check_and_connect_adb(device_id)
                if not connected:
                    macro_log(f"❌ [ADB 재연결 실패] {re_msg}")
                time.sleep(1.0)
                continue

            # 화면 암전(로딩 중) 체크: 최대 3회 재캡처 시도
            black_screen_retries = 0
            while (
                is_macro_running
                and float(np.mean(src_img)) < 15.0
                and black_screen_retries < 3
            ):
                black_screen_retries += 1
                macro_log(
                    f"[매크로] 화면 로딩 중(암전 상태) 감지 ({black_screen_retries}/3). 1초 대기 후 재캡처합니다..."
                )
                time.sleep(1.0)
                screenshot_file = capture_adb_screenshot(device_id)
                if screenshot_file and os.path.exists(screenshot_file):
                    new_img = imread_utf8(screenshot_file)
                    if new_img is not None:
                        src_img = new_img

            target_w = int(res_data.get("width", 1920))
            target_h = int(res_data.get("height", 1080))
            if src_img.shape[1] < src_img.shape[0]:
                macro_log(
                    "⚠️ [주의] 기기가 세로 모드로 감지되었습니다. 정상 인식을 위해 가로 모드(1920x1080)로 설정해주세요."
                )
            elif src_img.shape[1] != target_w or src_img.shape[0] != target_h:
                if loop_count == 1:
                    macro_log(
                        f"⚠️ [해상도 보정] 캡처 해상도({src_img.shape[1]}x{src_img.shape[0]})를 기준 해상도({target_w}x{target_h})에 맞춰 자동 보정합니다."
                    )
                src_img = cv2.resize(src_img, (target_w, target_h))

            result_roi_data = json.loads(res_data.get("result_roi") or "{}")
            card_size = result_roi_data.get("card_size", {"w": 110, "h": 230})
            slots = result_roi_data.get("slots", [])

            costume_roi = json.loads(res_data.get("costume_name_roi") or "{}")
            char_roi = json.loads(res_data.get("character_name_roi") or "{}")

            db_costumes = get_all_costumes_from_db()
            debug_log(
                f"총 {len(slots)}개 슬롯 판독 시작... (전역 인식률: {global_threshold})"
            )

            # 🌟 이번 회차의 판독 결과를 저장할 리스트와 카운터
            round_results = []
            five_star_count = 0  # 5성 카운트 집계용
            pulled_costume_names = (
                []
            )  # 이번 회차에서 획득한 코스튬 이름만 따로 저장 (조건 비교용)

            for i, slot in enumerate(slots):
                if not is_macro_running:
                    print("[매크로] 작업 도중 중지 요청 감지.")
                    break

                x, y = int(slot.get("x", 0)), int(slot.get("y", 0))
                w, h = int(card_size.get("w", 110)), int(card_size.get("h", 230))
                center_x, center_y = x + (w // 2), y + (h // 2)

                debug_log(f"--- [슬롯 {i+1}] 처리 --- (x:{x}, y:{y}, w:{w}, h:{h})")
                card_crop = src_img[y : y + h, x : x + w]

                # 공통 인식률 적용 매칭
                matched_name = match_costume_from_db(
                    card_crop, db_costumes, threshold=global_threshold
                )

                if matched_name:
                    # DB에서 해당 코스튬의 등급(rarity) 조회
                    rarity = 5
                    for c in db_costumes:
                        if c["costume_name"] == matched_name:
                            rarity = c.get("rarity", 5)
                            break

                    round_results.append(
                        format_slot_display(i + 1, matched_name, rarity)
                    )
                    pulled_costume_names.append(matched_name)  # 획득 코스튬 목록에 추가

                    if rarity == 5:
                        five_star_count += 1

                    print(f"   [슬롯 {i+1}] 매칭 성공 -> {matched_name}")

                    # 🌟 5성 코스튬인 경우 항상 OCR 테스트 실행 (WebUI 옵션 활성화 시)
                    if rarity == 5 and ocr_test_5star:
                        macro_log(
                            f"   [슬롯 {i+1}] 🔍 [5성 OCR 테스트] 정보창 열어 검증 진행: {matched_name}"
                        )
                        ocr_data = perform_slot_ocr(
                            device_id,
                            center_x,
                            center_y,
                            char_roi,
                            costume_roi,
                            click_delay,
                            slot_num=i + 1,
                        )
                        if ocr_data.get("success"):
                            ocr_full_name = ocr_data["full_costume_name"]
                            is_match = matched_name == ocr_full_name
                            match_str = (
                                "일치 ✅"
                                if is_match
                                else f"불일치 ⚠️ (매칭: {matched_name} vs OCR: {ocr_full_name})"
                            )
                            macro_log(f"   [슬롯 {i+1}] 📝 [5성 OCR 결과] {match_str}")
                            log_ocr_test(
                                loop_count,
                                i + 1,
                                matched_name,
                                ocr_data,
                                is_match=is_match,
                            )
                        else:
                            err_msg = ocr_data.get("error_msg", "인식 실패")
                            macro_log(f"   [슬롯 {i+1}] ❌ [5성 OCR 실패] {err_msg}")
                            log_ocr_test(
                                loop_count,
                                i + 1,
                                matched_name,
                                ocr_data,
                                is_match=False,
                                note=f"실패: {err_msg}",
                            )
                else:
                    if not auto_add_new:
                        round_results.append(f"[슬롯 {i+1}] ❓ 미인식 (저장됨)")
                        print(
                            f"   [슬롯 {i+1}] 매칭 실패 -> 자동 등록 비활성화 (이미지만 저장)"
                        )
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        unknown_filename = f"unknown_{timestamp}_slot{i+1}.png"
                        save_img_path = os.path.join(UNRECOGNIZED_DIR, unknown_filename)
                        save_cv2_image_utf8(save_img_path, card_crop)
                        continue

                    print(f"   [슬롯 {i+1}] 매칭 실패 -> 정보창 열어서 OCR 진행")
                    ocr_data = perform_slot_ocr(
                        device_id,
                        center_x,
                        center_y,
                        char_roi,
                        costume_roi,
                        click_delay,
                        slot_num=i + 1,
                    )

                    if not ocr_data.get("success"):
                        round_results.append(f"[슬롯 {i+1}] ❌ OCR 판독 실패 (스킵됨)")
                        print(f"   [슬롯 {i+1}] ❌ OCR 판독 실패 (저장 스킵)")
                        if ocr_test_5star:
                            log_ocr_test(
                                loop_count,
                                i + 1,
                                None,
                                ocr_data,
                                is_match=None,
                                note=f"신규 미인식 코스튬 OCR 실패: {ocr_data.get('error_msg', '')}",
                            )
                        continue

                    full_costume_name = ocr_data["full_costume_name"]

                    # 신규 등록 건은 기본 5성으로 간주하고 처리
                    round_results.append(
                        format_slot_display(i + 1, full_costume_name, 5, is_new=True)
                    )
                    pulled_costume_names.append(
                        full_costume_name
                    )  # 획득 코스튬 목록에 추가
                    five_star_count += 1

                    if ocr_test_5star:
                        log_ocr_test(
                            loop_count,
                            i + 1,
                            None,
                            ocr_data,
                            is_match=None,
                            note="신규 코스튬 OCR 감지 및 신규 등록",
                        )

                    image_filename = f"{full_costume_name}.png"
                    save_img_path = os.path.join(COSTUMES_DIR, image_filename)

                    conn = get_db_connection()
                    existing_costume = conn.execute(
                        "SELECT * FROM costumes WHERE costume_name = ?",
                        (full_costume_name,),
                    ).fetchone()

                    if existing_costume:
                        print(
                            f"   [슬롯 {i+1}] 이미 존재함 (이미지만 최신화): {full_costume_name}"
                        )
                        conn.close()
                        save_cv2_image_utf8(save_img_path, card_crop)
                        clear_template_cache()
                    else:
                        save_cv2_image_utf8(save_img_path, card_crop)
                        try:
                            # 개별 임계값 제거 후 INSERT
                            conn.execute(
                                "INSERT INTO costumes (costume_name, rarity, image_filename) VALUES (?, ?, ?)",
                                (full_costume_name, 5, save_img_path),
                            )
                            conn.commit()
                            clear_template_cache()
                            print(
                                f"   [슬롯 {i+1}] DB 신규 저장 성공: {full_costume_name}"
                            )
                        except Exception as db_err:
                            debug_log(f"[슬롯 {i+1}] DB 저장 실패: {db_err}")
                        finally:
                            conn.close()
                        db_costumes = get_all_costumes_from_db()

            # ==========================================
            # 1회차 슬롯 전체 판독 종료 후 요약 로그 및 [조건 검증]
            # ==========================================
            macro_log("-" * 35)
            macro_log(
                f"📊 [{loop_count}회차] 판독 결과 요약 (5성 획득: {five_star_count}개)"
            )
            for result_text in round_results:
                macro_log(f"  {result_text}")
            macro_log("-" * 35)

            # 1. 설정된 필터 및 목표 조건 가져오기
            filter_conf = config.get("filter", {})
            min_5star = int(filter_conf.get("min_5star", 0))
            need_5star = int(filter_conf.get("need_5star", 0))
            target_costumes = config.get("target_costumes", [])

            # 2. 이번 회차 데이터로 조건 분석
            desired_found_count = 0
            missing_required_list = []

            target_names = [tc.get("name") for tc in target_costumes]

            # 2-1. 획득한 희망 코스튬 갯수 집계
            for pulled in pulled_costume_names:
                if pulled in target_names:
                    desired_found_count += 1

            # 2-2. 필수 코스튬 누락 확인
            for tc in target_costumes:
                if tc.get("is_required", False):
                    if tc.get("name") not in pulled_costume_names:
                        missing_required_list.append(tc.get("name"))

            # 3. AND 조건 판별식
            cond1 = five_star_count >= min_5star
            cond2 = desired_found_count >= need_5star
            cond3 = len(missing_required_list) == 0

            # 4. 검증 결과 로그 출력 (사용자 확인용)
            macro_log(
                f"🎯 [조건 검증] 최소 5성 갯수 ({five_star_count}/{min_5star}) -> {'✅' if cond1 else '❌'}"
            )
            if target_costumes or need_5star > 0:
                macro_log(
                    f"🎯 [조건 검증] 희망 5성 갯수 ({desired_found_count}/{need_5star}) -> {'✅' if cond2 else '❌'}"
                )
            if any(tc.get("is_required") for tc in target_costumes):
                if cond3:
                    macro_log(f"🎯 [조건 검증] 필수 지정 코스튬 포함 여부 -> ✅")
                else:
                    macro_log(
                        f"🎯 [조건 검증] 필수 누락: {', '.join(missing_required_list)} -> ❌"
                    )

            # 5. 모든 조건 달성 시 처리 (재시도 / 종료 분기)
            if cond1 and cond2 and cond3:
                macro_log("🎉 [목표 달성] 설정한 모든 가챠 조건이 충족되었습니다!")

                discord_conf = config.get("discord", {})
                discord_enabled = discord_conf.get("enabled", False)
                share_complete = discord_conf.get("share_complete", False)

                user_decision = "stop"  # 기본값

                # 디스코드 연동 및 완료 알림이 활성화되어 있을 때
                if discord_enabled and share_complete:
                    discord_lines = []
                    for r in round_results:
                        if "[5성]" in r:
                            discord_lines.append(f"• **{r}**")
                        else:
                            discord_lines.append(f"• {r}")
                    round_details = "\n".join(discord_lines)
                    prompt_text = (
                        "🎉 **[가챠 목표 달성] 조건에 맞는 가챠 결과가 나왔습니다!**\n\n"
                        "💬 **다음 작업을 디스코드 채팅으로 입력해 주세요:**\n"
                        "• `재시도` (또는 `1`) : 이 결과를 넘기고 가챠를 계속 진행합니다.\n"
                        "• `종료` (또는 `2`)   : 이 결과로 가챠를 확정하고 매크로를 종료합니다.\n"
                        "*(이 메시지의 🔄 또는 ⏹️ 이모지를 누르셔도 됩니다 / 5분 뒤 자동 종료)*"
                    )

                    success, resp_data = send_discord_notification(
                        config,
                        content=prompt_text,
                        embed_title=f"🏆 가챠 결과 요약 ({loop_count}회차 달성 / 5성 {five_star_count}개)",
                        embed_desc=round_details,
                        image_path=screenshot_file,
                        wait=True,
                    )

                    if success and isinstance(resp_data, dict) and resp_data.get("id"):
                        msg_id = resp_data["id"]
                        # 🔄 / ⏹️ 리액션 자동 추가 (백그라운드 스레드)
                        threading.Thread(
                            target=add_discord_reactions,
                            args=(
                                discord_conf.get("bot_token"),
                                discord_conf.get("channel_id"),
                                msg_id,
                            ),
                            daemon=True,
                        ).start()

                        # 사용자의 디스코드 응답 대기 (최대 5분)
                        user_decision = wait_for_discord_decision(
                            config, msg_id, timeout_sec=300
                        )

                if user_decision == "retry":
                    macro_log(
                        "🔄 [디스코드 명령] '재시도'가 선택되었습니다. 가챠를 계속 진행합니다."
                    )
                    send_discord_notification(
                        config,
                        content="🔄 **[재시도]** 사용자의 선택에 따라 다음 가챠를 계속 진행합니다!",
                        wait=False,
                    )
                    time.sleep(click_delay)
                    continue
                else:
                    macro_log(
                        "⏹️ [디스코드] '종료'가 선택되었습니다. 매크로를 안전하게 종료합니다."
                    )
                    if discord_enabled and share_complete:
                        send_discord_notification(
                            config,
                            content="⏹️ **[매크로 종료]** 가챠 결과를 확정하고 매크로를 완전히 종료했습니다.",
                            wait=False,
                        )
                    is_macro_running = False

                    # 🌟 PC 자동 종료 옵션
                    if options.get("shutdown_pc_on_completion"):
                        macro_log(
                            "💻 [시스템] 설정에 따라 60초 후 PC를 자동 종료합니다."
                        )
                        subprocess.run("shutdown /s /t 60", shell=True)

                    break
            else:
                macro_log("🔄 조건 미달. 다음 가챠 회차를 준비합니다.")

            macro_log(f"[매크로] {loop_count}회차 가챠 처리 완료.")
            time.sleep(click_delay)

        except Exception as e:
            macro_log(f"[매크로 예외 에러]: {e}")
            # 🌟 디스코드 오류 알림 전송
            try:
                discord_conf = config.get("discord", {})
                if discord_conf.get("enabled") and discord_conf.get("share_error"):
                    send_discord_notification(
                        config,
                        content=f"⚠️ **[매크로 예외 에러 발생]**\n```{str(e)}```",
                        embed_title=f"⚠️ 에러 알림 ({loop_count}회차 진행 중)",
                        embed_desc=f"오류 내용: {str(e)}",
                    )
            except Exception:
                pass
            time.sleep(1.0)

    if auto_fhd_applied:
        macro_log("📱 [해상도 복구] 기기 화면 해상도를 원래대로 복원합니다.")
        reset_device_resolution(device_id)

    is_macro_running = False
    macro_log("[매크로] 백그라운드 루프가 완전 종료되었습니다.")


# ==============================================================================
# 6. Flask 라우트 (웹 서버 API)
# ==============================================================================
@app.route("/")
def index():
    return render_template("index.html", version=APP_VERSION)


@app.route("/api/version", methods=["GET"])
def get_version():
    return jsonify({"version": APP_VERSION})


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        app.static_folder, "favicon.ico", mimetype="image/vnd.microsoft.icon"
    )


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def update_config():
    try:
        save_config_with_backup(request.json)
        return jsonify(
            {"status": "success", "message": "설정이 백업 후 저장되었습니다."}
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/backups", methods=["GET"])
def list_backups():
    try:
        files = [
            f
            for f in os.listdir(BACKUP_DIR)
            if f.startswith("config_") and f.endswith(".yaml")
        ]
        files.sort(reverse=True)
        return jsonify({"status": "success", "files": files})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/config/restore", methods=["POST"])
def restore_backup():
    try:
        filename = request.json.get("filename")
        if not filename:
            return (
                jsonify({"status": "error", "message": "파일명이 누락되었습니다."}),
                400,
            )

        backup_path = os.path.join(BACKUP_DIR, filename)
        if not os.path.exists(backup_path):
            return (
                jsonify(
                    {"status": "error", "message": "백업 파일이 존재하지 않습니다."}
                ),
                404,
            )

        restored_data = load_config(backup_path)
        save_config_with_backup(restored_data)
        return jsonify(
            {
                "status": "success",
                "message": f"[{filename}] 데이터로 복원되었습니다.",
                "config": restored_data,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/run-action", methods=["POST"])
def run_action():
    global is_macro_running, macro_thread
    try:
        action = (request.json or {}).get("action")
        if action == "start":
            if is_macro_running:
                return (
                    jsonify(
                        {"status": "error", "message": "매크로가 이미 실행 중입니다."}
                    ),
                    400,
                )
            is_macro_running = True
            macro_thread = threading.Thread(target=run_gacha_macro_loop, daemon=True)
            macro_thread.start()
            return jsonify({"status": "success", "message": "매크로가 시작되었습니다."})
        elif action == "stop":
            if not is_macro_running:
                return (
                    jsonify(
                        {"status": "error", "message": "실행 중인 매크로가 없습니다."}
                    ),
                    400,
                )
            is_macro_running = False
            return jsonify(
                {
                    "status": "success",
                    "message": "매크로 중지 요청됨 (현재 진행 중인 회차 종료 후 멈춤).",
                }
            )
        return (
            jsonify({"status": "error", "message": "알 수 없는 작업 요청입니다."}),
            400,
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/adb/devices", methods=["GET"])
def adb_devices_list():
    """연결된 모든 ADB 기기(앱플레이어, 실제 USB 스마트폰/태블릿 등) 목록 반환"""
    try:
        devices = get_connected_devices()
        return jsonify({"status": "success", "devices": devices})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/adb/device-resolution", methods=["GET"])
def adb_get_device_resolution():
    """현재 선택된 타깃 기기의 해상도 정보 조회"""
    try:
        config = load_config()
        device_id = get_target_device_id(config)
        res_info = check_resolution_match(device_id)
        return jsonify(
            {
                "status": "success",
                "device_id": device_id,
                "physical_size": res_info["physical_size"],
                "override_size": res_info["override_size"],
                "current_size": res_info["device_res"],
                "target_resolution": res_info["target_res"],
                "is_emulator": res_info["is_emulator"],
                "is_matched": res_info["matched"],
                "is_portrait": res_info.get("is_portrait", False),
                "message": res_info["message"],
                "is_fhd": res_info["matched"],
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/adb/device-resolution/fhd", methods=["POST"])
def adb_set_device_resolution_fhd():
    """사용자가 직접 선택하여 16:9 FHD(1080x1920) 해상도로 변경"""
    try:
        config = load_config()
        device_id = get_target_device_id(config)
        success = set_device_resolution(device_id, 1080, 1920, 400)
        if success:
            macro_log(
                f"📱 수동 설정: [{device_id}] 해상도를 1080x1920 (FHD)로 맞추었습니다."
            )
            return jsonify(
                {
                    "status": "success",
                    "message": f"기기({device_id}) 해상도가 1080x1920 (FHD)로 변경되었습니다.",
                }
            )
        return (
            jsonify({"status": "error", "message": "해상도 변경 명령 실행 실패"}),
            500,
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/adb/device-resolution/reset", methods=["POST"])
def adb_reset_device_resolution():
    """사용자가 직접 원래 기기 해상도로 복원"""
    try:
        config = load_config()
        device_id = get_target_device_id(config)
        success = reset_device_resolution(device_id)
        if success:
            macro_log(
                f"📱 수동 설정: [{device_id}] 해상도를 원래 기본값으로 복원했습니다."
            )
            return jsonify(
                {
                    "status": "success",
                    "message": f"기기({device_id}) 해상도가 원래 기본값으로 복원되었습니다.",
                }
            )
        return (
            jsonify({"status": "error", "message": "해상도 복원 명령 실행 실패"}),
            500,
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/adb/connect", methods=["POST"])
def adb_connect():
    try:
        config = load_config()
        device_id = get_target_device_id(config)
        connected, msg = check_and_connect_adb(device_id)

        if connected:
            return jsonify(
                {"status": "success", "message": f"ADB 장치 ({device_id}) 연결 성공!"}
            )
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"ADB 장치 ({device_id}) 연결 실패: {msg}",
                }
            ),
            400,
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/adb/click", methods=["POST"])
def adb_click():
    try:
        data = request.json or {}
        x, y = data.get("x"), data.get("y")
        if x is None or y is None:
            return jsonify({"status": "error", "message": "X, Y 좌표 필요"}), 400

        config = load_config()
        device_id = get_target_device_id(config)
        run_adb_cmd(
            ["-s", device_id, "shell", "input", "tap", str(int(x)), str(int(y))],
            timeout=10,
        )
        return jsonify({"status": "success", "message": f"좌표 ({x}, {y}) 클릭"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/adb/disconnect", methods=["POST"])
def adb_disconnect():
    try:
        config = load_config()
        device_id = get_target_device_id(config)
        if ":" in device_id or device_id.startswith("127.0.0.1"):
            result = run_adb_cmd(["disconnect", device_id], timeout=10)
            return jsonify(
                {
                    "status": "success",
                    "message": f"ADB 네트워크 장치 ({device_id}) 연결 해제됨",
                    "output": result.stdout.strip(),
                }
            )
        else:
            return jsonify(
                {
                    "status": "success",
                    "message": f"USB 연결 장치 ({device_id})는 PC 케이블 분리 시 연결이 해제됩니다.",
                }
            )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/db-data", methods=["GET"])
def get_db_data():
    try:
        conn = get_db_connection()
        resolutions = conn.execute("SELECT * FROM resolutions").fetchall()
        costumes = conn.execute(
            "SELECT * FROM costumes WHERE rarity = 5 ORDER BY costume_name ASC"
        ).fetchall()
        conn.close()

        costume_list = [dict(c, full_name=c["costume_name"]) for c in costumes]
        return jsonify(
            {
                "status": "success",
                "resolutions": [dict(r) for r in resolutions],
                "costumes": costume_list,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/db/resolutions", methods=["POST"])
def add_resolution():
    try:
        data = request.json
        if not data:
            return (
                jsonify({"status": "error", "message": "데이터가 누락되었습니다."}),
                400,
            )

        try:
            w = int(data.get("width") or 0)
            h = int(data.get("height") or 0)
            dpi = int(data.get("dpi") or 0)
        except (ValueError, TypeError):
            return (
                jsonify(
                    {"status": "error", "message": "너비, 높이, DPI는 정수여야 합니다."}
                ),
                400,
            )

        name = str(data.get("name") or "").strip()
        if not w or not h or not name:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "너비, 높이, 이름은 필수 입력 항목입니다.",
                    }
                ),
                400,
            )

        conn = get_db_connection()
        # 기존 레코드 존재 여부 확인
        existing = conn.execute(
            "SELECT * FROM resolutions WHERE width = ? AND height = ? AND dpi = ?",
            (w, h, dpi),
        ).fetchone()

        char_roi = data.get("character_name_roi")
        costume_roi = data.get("costume_name_roi")
        if existing:
            if not char_roi:
                char_roi = existing["character_name_roi"] or "{}"
            if not costume_roi:
                costume_roi = existing["costume_name_roi"] or "{}"
        else:
            char_roi = char_roi or "{}"
            costume_roi = costume_roi or "{}"

        conn.execute(
            """INSERT OR REPLACE INTO resolutions
               (width, height, dpi, name, gacha_btn_pos, skip_btn_pos, confirm_btn_pos, result_roi, screenshot_roi, character_name_roi, costume_name_roi)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                w,
                h,
                dpi,
                name,
                data.get("gacha_btn_pos") or "{}",
                data.get("skip_btn_pos") or "{}",
                data.get("confirm_btn_pos") or "{}",
                data.get("result_roi") or "{}",
                data.get("screenshot_roi") or "{}",
                char_roi,
                costume_roi,
            ),
        )
        conn.commit()
        conn.close()
        return jsonify(
            {
                "status": "success",
                "message": f"해상도 [{name}] 데이터가 추가/수정되었습니다.",
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/db/resolutions/delete", methods=["POST"])
def delete_resolution():
    try:
        data = request.json
        if not data:
            return (
                jsonify({"status": "error", "message": "데이터가 누락되었습니다."}),
                400,
            )
        w = int(data.get("width") or 0)
        h = int(data.get("height") or 0)
        dpi = int(data.get("dpi") or 0)

        conn = get_db_connection()
        conn.execute(
            "DELETE FROM resolutions WHERE width = ? AND height = ? AND dpi = ?",
            (w, h, dpi),
        )
        conn.commit()
        conn.close()
        return jsonify(
            {"status": "success", "message": "해상도 데이터가 삭제되었습니다."}
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/db/costumes", methods=["POST"])
def save_costume():
    """코스튬 데이터 추가 및 수정 (id 기준 우선, 없을 시 이름 기준 업데이트)"""
    try:
        data = request.json
        costume_id = data.get("id")
        costume_name = data.get("costume_name", "").strip()
        rarity = int(data.get("rarity", 5))  # 프론트에서 넘어온 레어도 값 추출
        new_image_path = data.get("image_filename", "").strip()

        if not costume_name:
            return (
                jsonify(
                    {"status": "error", "message": "코스튬명은 비워둘 수 없습니다."}
                ),
                400,
            )

        conn = get_db_connection()

        if costume_id:
            # 1. ID 기준으로 수정
            old_data = conn.execute(
                "SELECT image_filename FROM costumes WHERE id = ?", (costume_id,)
            ).fetchone()
            if old_data:
                old_image_path = old_data["image_filename"]
                if (
                    old_image_path
                    and new_image_path
                    and (old_image_path != new_image_path)
                ):
                    if os.path.exists(old_image_path):
                        try:
                            os.makedirs(
                                os.path.dirname(new_image_path) or ".", exist_ok=True
                            )
                            shutil.move(old_image_path, new_image_path)
                            debug_log(
                                f"실제 파일 이름 변경 완료: {old_image_path} -> {new_image_path}"
                            )
                        except Exception as file_err:
                            debug_log(f"실제 파일 이름 변경 실패: {file_err}")

            conn.execute(
                """
                UPDATE costumes
                SET costume_name = ?, rarity = ?, image_filename = ?
                WHERE id = ?
                """,
                (costume_name, rarity, new_image_path, costume_id),
            )
        else:
            # ID가 없는 경우, 코스튬 이름이 이미 DB에 존재하는지 확인
            existing = conn.execute(
                "SELECT id, rarity FROM costumes WHERE costume_name = ?",
                (costume_name,),
            ).fetchone()

            if existing:
                # 이미 존재하는 코스튬 이미지 갱신 시: 기존 성급(rarity)은 보존하고 이미지만 업데이트
                preserved_rarity = existing["rarity"]
                conn.execute(
                    """
                    UPDATE costumes
                    SET image_filename = ?
                    WHERE id = ?
                    """,
                    (new_image_path, existing["id"]),
                )
                debug_log(
                    f"코스튬 이미지 갱신 완료: {costume_name} (기존 {preserved_rarity}성 보존)"
                )
            else:
                # 완전 신규 등록
                conn.execute(
                    """
                    INSERT INTO costumes (costume_name, rarity, image_filename)
                    VALUES (?, ?, ?)
                    """,
                    (costume_name, rarity, new_image_path),
                )

        conn.commit()
        conn.close()
        clear_template_cache()
        return jsonify(
            {
                "status": "success",
                "message": "코스튬 데이터가 성공적으로 저장/수정되었습니다.",
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/db/costumes/delete", methods=["POST"])
def delete_costume():
    try:
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM costumes WHERE costume_name = ?",
            (request.json["costume_name"],),
        )
        conn.commit()
        conn.close()
        clear_template_cache()
        return jsonify(
            {"status": "success", "message": "코스튬 데이터가 삭제되었습니다."}
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/db/corrections", methods=["GET"])
def get_corrections():
    try:
        conn = get_db_connection()
        char_corrs = [
            dict(r) for r in conn.execute("SELECT * FROM char_corrections").fetchall()
        ]
        costume_corrs = [
            dict(r)
            for r in conn.execute("SELECT * FROM costume_corrections").fetchall()
        ]
        conn.close()
        return jsonify(
            {
                "status": "success",
                "char_corrections": char_corrs,
                "costume_corrections": costume_corrs,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/db/corrections", methods=["POST"])
def add_correction():
    try:
        data = request.json
        table_name = (
            "char_corrections" if data.get("type") == "char" else "costume_corrections"
        )
        conn = get_db_connection()
        conn.execute(
            f"INSERT OR REPLACE INTO {table_name} (wrong_text, correct_text) VALUES (?, ?)",
            (data.get("wrong_text").strip(), data.get("correct_text").strip()),
        )
        conn.commit()
        conn.close()
        return jsonify(
            {
                "status": "success",
                "message": f"보정 단어 [{data.get('wrong_text')}] 저장 완료.",
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/db/corrections/delete", methods=["POST"])
def delete_correction():
    try:
        data = request.json
        table_name = (
            "char_corrections" if data.get("type") == "char" else "costume_corrections"
        )
        conn = get_db_connection()
        conn.execute(
            f"DELETE FROM {table_name} WHERE wrong_text = ?", (data.get("wrong_text"),)
        )
        conn.commit()
        conn.close()
        return jsonify(
            {
                "status": "success",
                "message": f"보정 단어 [{data.get('wrong_text')}] 삭제 완료.",
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/unrecognized/list", methods=["GET"])
def list_unrecognized_images():
    """unrecognized 폴더에 쌓인 미인식 이미지 파일 리스트 반환"""
    try:
        if not os.path.exists(UNRECOGNIZED_DIR):
            return jsonify({"status": "success", "files": []})

        files = [
            f
            for f in os.listdir(UNRECOGNIZED_DIR)
            if f.lower().endswith((".png", ".jpg", ".jpeg"))
        ]
        files.sort(reverse=True)  # 최신 파일이 위로 오도록 정렬
        return jsonify({"status": "success", "files": files})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/static/unrecognized/<path:filename>")
def serve_unrecognized_image(filename):
    """웹 UI에서 미인식 이미지를 미리보기 할 수 있도록 서빙하는 경로"""
    return send_from_directory(UNRECOGNIZED_DIR, filename)


@app.route("/api/unrecognized/delete", methods=["POST"])
def delete_unrecognized():
    """미인식 폴더의 특정 이미지 파일을 삭제"""
    try:
        data = request.json
        filename = data.get("filename")
        if not filename:
            return (
                jsonify(
                    {"status": "error", "message": "파일명이 제공되지 않았습니다."}
                ),
                400,
            )

        file_path = os.path.join(UNRECOGNIZED_DIR, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            debug_log(f"미인식 이미지 삭제 완료: {filename}")
            return jsonify({"status": "success", "message": "이미지가 삭제되었습니다."})
        else:
            return (
                jsonify(
                    {"status": "error", "message": "해당 파일을 찾을 수 없습니다."}
                ),
                404,
            )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/unrecognized/register", methods=["POST"])
def register_unrecognized():
    try:
        data = request.json
        filename = data.get("filename")
        char_name = data.get("char_name", "").strip()
        costume_name = data.get("costume_name", "").strip()
        rarity = int(data.get("rarity", 5))  # 셀렉트박스에서 넘겨준 레어도 값

        if not char_name or not costume_name:
            return jsonify(
                {"status": "error", "message": "캐릭터명과 코스튬명을 입력하세요."}
            )

        full_name = f"{char_name}_{costume_name}"
        old_path = os.path.join(UNRECOGNIZED_DIR, filename)
        new_filename = f"{full_name}.png"
        new_path = os.path.join(COSTUMES_DIR, new_filename)
        rel_db_path = os.path.join("costumes", new_filename)

        if not os.path.exists(old_path):
            return jsonify(
                {"status": "error", "message": "해당 미인식 파일이 존재하지 않습니다."}
            )

        # unrecognized 폴더에서 costumes 폴더로 파일 이동
        shutil.move(old_path, new_path)

        # DB에 신규 레어도 반영하여 등록
        conn = get_db_connection()
        existing = conn.execute(
            "SELECT id, rarity FROM costumes WHERE costume_name = ?", (full_name,)
        ).fetchone()

        if existing:
            # 이미 존재하는 코스튬 이미지 갱신 시: 기존 성급(rarity)은 보존하고 이미지만 갱신
            preserved_rarity = existing["rarity"]
            conn.execute(
                "UPDATE costumes SET image_filename = ? WHERE id = ?",
                (rel_db_path, existing["id"]),
            )
            conn.commit()
            conn.close()
            clear_template_cache()

            debug_log(
                f"미인식 이미지 갱신 완료: {full_name} (기존 {preserved_rarity}성 보존)"
            )
            return jsonify(
                {
                    "status": "success",
                    "message": f"{full_name} (기존 {preserved_rarity}성 유지) 이미지가 성공적으로 갱신되었습니다.",
                }
            )
        else:
            # 없으면 새로 추가
            conn.execute(
                "INSERT INTO costumes (costume_name, rarity, image_filename) VALUES (?, ?, ?)",
                (full_name, rarity, rel_db_path),
            )
            conn.commit()
            conn.close()
            clear_template_cache()

            debug_log(f"미인식 이미지 등록 완료: {full_name} ({rarity}성)")
            return jsonify(
                {
                    "status": "success",
                    "message": f"{full_name} ({rarity}성) 등록이 완료되었습니다.",
                }
            )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/logs/poll", methods=["GET"])
def poll_web_logs():
    global web_logs_queue, is_macro_running
    logs_to_send = list(web_logs_queue)
    web_logs_queue.clear()
    return jsonify(
        {"status": "success", "logs": logs_to_send, "is_running": is_macro_running}
    )


@app.route("/api/ocr-test-log", methods=["GET"])
def get_ocr_test_log():
    """ocr_test.log 파일의 내용을 읽어 반환"""
    try:
        if not os.path.exists(OCR_TEST_LOG_PATH):
            return jsonify(
                {
                    "status": "success",
                    "content": "아직 기록된 5성 OCR 테스트 로그가 없습니다.",
                    "exists": False,
                }
            )

        with open(OCR_TEST_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        max_lines = request.args.get("lines", type=int, default=300)
        recent_lines = lines[-max_lines:] if len(lines) > max_lines else lines
        return jsonify(
            {
                "status": "success",
                "content": "".join(recent_lines),
                "total_lines": len(lines),
                "exists": True,
            }
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ocr-test-log/clear", methods=["POST"])
def clear_ocr_test_log():
    """ocr_test.log 파일 내용 비우기"""
    try:
        if os.path.exists(OCR_TEST_LOG_PATH):
            with open(OCR_TEST_LOG_PATH, "w", encoding="utf-8") as f:
                f.write("")
        return jsonify(
            {"status": "success", "message": "ocr_test.log 파일이 초기화되었습니다."}
        )
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/discord/test", methods=["POST"])
def test_discord():
    """디스코드 봇 메시지 전송 테스트"""
    try:
        data = request.json or {}
        bot_token = data.get("bot_token", "").strip()
        channel_id = data.get("channel_id", "").strip()
        user_id = data.get("user_id", "").strip()

        if not bot_token:
            return (
                jsonify({"status": "error", "message": "봇 토큰을 입력해 주세요."}),
                400,
            )
        if not channel_id:
            return (
                jsonify({"status": "error", "message": "채널 ID를 입력해 주세요."}),
                400,
            )

        test_config = {
            "discord": {
                "enabled": True,
                "bot_token": bot_token,
                "channel_id": channel_id,
                "user_id": user_id,
            }
        }

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        success, msg = send_discord_notification(
            test_config,
            content="🔔 **[BD2 Controller] 디스코드 봇 연동 테스트 메시지입니다!**",
            embed_title="✅ 디스코드 봇 연결 성공",
            embed_desc=f"정상적으로 메시지를 수신했습니다.\n- 전송 시간: {now_str}\n- 채널 ID: `{channel_id}`",
            wait=True,
        )

        if success:
            return jsonify(
                {
                    "status": "success",
                    "message": "디스코드 테스트 메시지를 성공적으로 전송했습니다!",
                }
            )
        else:
            return jsonify({"status": "error", "message": f"전송 실패: {msg}"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ==============================================================================
# 7. 실행 영역 (Entry Point)
# ==============================================================================
def open_browser():
    webbrowser.open_new("http://127.0.0.1:5000/")


if __name__ == "__main__":
    print(f"[*] BD2 Controller v{APP_VERSION} 시작...")
    Timer(1.5, open_browser).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
