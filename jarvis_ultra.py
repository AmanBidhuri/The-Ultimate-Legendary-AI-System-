#!/usr/bin/env python3
"""Jarvis Ultra for Android + Termux + Termux:API.

What this script is designed for
- Voice-driven phone control from Termux.
- Safer automation than the earlier demo version.
- Persistent notes, reminders, app launching, search, calls, SMS.
- Confirmation flow for risky actions.

What it intentionally does NOT promise
- True Iron Man level always-on wake-word detection with zero extra setup.
- Full Android UI automation or guaranteed app closing on every device.
- Offline speech recognition without extra native ML packages.

Recommended install
    pkg update -y
    pkg upgrade -y
    pkg install -y python termux-api

Required Android permissions for Termux and Termux:API
- Microphone
- Phone
- SMS
- Contacts
- Storage
- Notifications

Run
    python jarvis_ultra.py

Optional files
- ~/.jarvis_ultra/contacts.json
- ~/.jarvis_ultra/notes.txt
- ~/.jarvis_ultra/reminders.json
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote_plus

APP_NAME = "Jarvis Ultra"
WAKE_WORDS = ("jarvis", "hey jarvis", "ok jarvis", "okay jarvis")
DATA_DIR = Path.home() / ".jarvis_ultra"
CONTACTS_FILE = DATA_DIR / "contacts.json"
NOTES_FILE = DATA_DIR / "notes.txt"
REMINDERS_FILE = DATA_DIR / "reminders.json"
LOG_FILE = DATA_DIR / "jarvis.log"

APP_MAP = {
    "whatsapp": "com.whatsapp",
    "chrome": "com.android.chrome",
    "google chrome": "com.android.chrome",
    "youtube": "com.google.android.youtube",
    "yt": "com.google.android.youtube",
    "gmail": "com.google.android.gm",
    "maps": "com.google.android.apps.maps",
    "camera": "com.android.camera",
    "settings": "com.android.settings",
    "telegram": "org.telegram.messenger",
    "instagram": "com.instagram.android",
    "facebook": "com.facebook.katana",
    "spotify": "com.spotify.music",
    "files": "com.android.documentsui",
}

RISKY_CONFIRMATIONS = {
    "call",
    "sms",
    "message",
    "wifi",
    "flashlight",
    "clear notes",
    "delete notes",
    "erase notes",
    "force close",
}


@dataclass
class Reminder:
    id: int
    when: str  # ISO format
    text: str
    fired: bool = False

    @property
    def when_dt(self) -> datetime:
        return datetime.fromisoformat(self.when)


@dataclass
class PendingAction:
    name: str
    payload: dict
    created_at: float


class JarvisUltra:
    def __init__(self) -> None:
        self.data_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.pending_action: Optional[PendingAction] = None
        self.reminder_thread: Optional[threading.Thread] = None
        self.reminder_queue: queue.Queue[str] = queue.Queue()
        self._ensure_files()
        self._check_environment()

    # ---------- infrastructure ----------
    def _ensure_files(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not CONTACTS_FILE.exists():
            CONTACTS_FILE.write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")
        if not REMINDERS_FILE.exists():
            REMINDERS_FILE.write_text("[]", encoding="utf-8")
        if not NOTES_FILE.exists():
            NOTES_FILE.write_text("", encoding="utf-8")

    def _check_environment(self) -> None:
        missing = []
        for cmd in (
            "termux-tts-speak",
            "termux-speech-to-text",
            "termux-toast",
            "termux-notification",
            "termux-battery-status",
            "termux-open-url",
        ):
            if shutil.which(cmd) is None:
                missing.append(cmd)
        if missing:
            self.log(f"Missing commands: {', '.join(missing)}")

    def log(self, message: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
        print(line)
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "
")
        except Exception:
            pass

    def run(self, args: list[str], check: bool = False, input_text: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args,
                input=input_text,
                capture_output=True,
                text=True,
                check=check,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Command not found: {args[0]}") from exc

    def run_out(self, args: list[str]) -> str:
        try:
            proc = self.run(args)
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            return out if out else err
        except Exception as exc:
            return str(exc)

    def speak(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        self.log(f"SAY: {text}")
        try:
            self.run(["termux-tts-speak", text])
        except Exception:
            pass

    def toast(self, text: str) -> None:
        try:
            self.run(["termux-toast", text])
        except Exception:
            pass

    def notify(self, title: str, content: str = "") -> None:
        try:
            self.run(["termux-notification", "--title", title, "--content", content])
        except Exception:
            pass

    def listen(self) -> str:
        """Listen using Android's speech service. Falls back to typing if needed."""
        try:
            text = self.run_out(["termux-speech-to-text"]).strip()
            if text:
                return text
        except Exception as exc:
            self.log(f"Speech error: {exc}")

        try:
            return input("Type command > ").strip()
        except EOFError:
            return ""

    def open_url(self, url: str) -> None:
        self.run(["termux-open-url", url])

    def open_app(self, package_name: str) -> None:
        # Best-effort launcher.
        self.run(["monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"])

    def force_close_app(self, package_name: str) -> None:
        # This may require shell privileges on some phones. Best-effort only.
        self.run(["am", "force-stop", package_name])

    def call_number(self, number: str) -> None:
        self.run(["termux-telephony-call", number])

    def send_sms(self, number: str, message: str) -> None:
        self.run(["termux-sms-send", "-n", number, message])

    def battery_status(self) -> dict | None:
        raw = self.run_out(["termux-battery-status"])
        try:
            return json.loads(raw)
        except Exception:
            return None

    def wifi_toggle(self, state: bool) -> None:
        self.run(["termux-wifi-enable", "true" if state else "false"])

    def flashlight_toggle(self, state: bool) -> None:
        # Termux:API supports torch on many devices.
        self.run(["termux-torch", "on" if state else "off"])

    def volume_set(self, stream: str, level: int) -> None:
        level = max(0, min(15, int(level)))
        self.run(["termux-volume", stream, str(level)])

    def brightness_set(self, level: int) -> None:
        # 0-255 on many Android devices.
        level = max(0, min(255, int(level)))
        self.run(["termux-brightness", str(level)])

    # ---------- persistence ----------
    def load_contacts(self) -> dict[str, str]:
        try:
            return json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_contacts(self, data: dict[str, str]) -> None:
        CONTACTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_notes(self) -> list[str]:
        if not NOTES_FILE.exists():
            return []
        return [line.strip() for line in NOTES_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]

    def add_note(self, text: str) -> None:
        with NOTES_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | {text.strip()}
")

    def clear_notes(self) -> None:
        NOTES_FILE.write_text("", encoding="utf-8")

    def load_reminders(self) -> list[Reminder]:
        try:
            raw = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
            return [Reminder(**item) for item in raw]
        except Exception:
            return []

    def save_reminders(self, reminders: list[Reminder]) -> None:
        REMINDERS_FILE.write_text(
            json.dumps([asdict(r) for r in reminders], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- helpers ----------
    def normalize(self, text: str) -> str:
        text = (text or "").strip().lower()
        text = re.sub(r"\s+", " ", text)
        return text

    def strip_wake_word(self, text: str) -> str:
        text = self.normalize(text)
        for ww in WAKE_WORDS:
            if text.startswith(ww):
                text = text[len(ww):].strip(" ,:-")
                break
        return text

    def extract_number(self, text: str) -> str:
        digits = re.sub(r"[^\d+]", "", text)
        return digits

    def resolve_contact(self, query: str) -> Optional[str]:
        query = self.normalize(query)
        contacts = self.load_contacts()

        # Exact alias match.
        for name, number in contacts.items():
            if self.normalize(name) == query:
                return number

        # Fuzzy containment.
        for name, number in contacts.items():
            if query in self.normalize(name) or self.normalize(name) in query:
                return number

        # Try system contacts if available.
        if shutil.which("termux-contact-list"):
            raw = self.run_out(["termux-contact-list"])
            try:
                entries = json.loads(raw)
                for entry in entries:
                    name = self.normalize(str(entry.get("name", "")))
                    phones = entry.get("number") or entry.get("numbers") or []
                    if isinstance(phones, str):
                        phones = [phones]
                    if query and query in name:
                        for ph in phones:
                            if ph:
                                return str(ph)
            except Exception:
                pass

        return None

    def parse_duration(self, text: str) -> Optional[timedelta]:
        text = self.normalize(text)
        patterns = [
            (r"(\d+)\s*sec(?:ond)?s?", "seconds"),
            (r"(\d+)\s*min(?:ute)?s?", "minutes"),
            (r"(\d+)\s*hour(?:s)?", "hours"),
            (r"(\d+)\s*day(?:s)?", "days"),
        ]
        total = timedelta()
        matched = False
        for pattern, unit in patterns:
            for num in re.findall(pattern, text):
                matched = True
                n = int(num)
                if unit == "seconds":
                    total += timedelta(seconds=n)
                elif unit == "minutes":
                    total += timedelta(minutes=n)
                elif unit == "hours":
                    total += timedelta(hours=n)
                elif unit == "days":
                    total += timedelta(days=n)
        return total if matched and total.total_seconds() > 0 else None

    def ask_confirm(self, action_name: str, payload: dict) -> None:
        self.pending_action = PendingAction(name=action_name, payload=payload, created_at=time.time())
        self.speak(f"{action_name} karna hai. Confirm karne ke liye yes bolo, cancel ke liye no bolo.")

    def handle_pending(self, cmd: str) -> bool:
        if not self.pending_action:
            return False
        cmd = self.normalize(cmd)
        if cmd in {"yes", "haan", "ha", "confirm", "ok", "okay", "karo", "kar do"}:
            action = self.pending_action
            self.pending_action = None
            self.execute_confirmed_action(action)
            return True
        if cmd in {"no", "nahin", "nahi", "cancel", "mat karo", "stop"}:
            self.pending_action = None
            self.speak("Theek hai, cancel kar diya.")
            return True
        # expire after 25 seconds
        if time.time() - self.pending_action.created_at > 25:
            self.pending_action = None
            self.speak("Confirmation timeout ho gaya.")
        return False

    def execute_confirmed_action(self, action: PendingAction) -> None:
        name = action.name
        payload = action.payload
        if name == "call":
            self.call_number(payload["number"])
            self.speak(f"Calling {payload.get('label', payload['number'])}.")
        elif name == "sms":
            self.send_sms(payload["number"], payload["message"])
            self.speak("Message bhej diya.")
        elif name == "wifi on":
            self.wifi_toggle(True)
            self.speak("Wi-Fi on kar diya.")
        elif name == "wifi off":
            self.wifi_toggle(False)
            self.speak("Wi-Fi off kar diya.")
        elif name == "flashlight on":
            self.flashlight_toggle(True)
            self.speak("Torch on kar diya.")
        elif name == "flashlight off":
            self.flashlight_toggle(False)
            self.speak("Torch off kar diya.")
        elif name == "clear notes":
            self.clear_notes()
            self.speak("Notes clear kar diye.")
        elif name == "force close":
            self.force_close_app(payload["package"])
            self.speak(f"{payload.get('label', 'app')} close karne ki koshish ki.")

    # ---------- command handlers ----------
    def handle_command(self, raw_cmd: str) -> None:
        if not raw_cmd:
            return

        cmd = self.strip_wake_word(raw_cmd)
        if not cmd:
            self.speak("Haan boss?")
            return

        if self.handle_pending(cmd):
            return

        # Core exits
        if cmd in {"exit", "quit", "band", "stop", "shutdown", "goodbye", "bye"}:
            self.speak("Jarvis offline.")
            self.stop_event.set()
            return

        # Help
        if cmd in {"help", "commands", "madad", "what can you do"}:
            self.speak(
                "Main time, battery, search, YouTube, app open, call, SMS, notes, reminders, volume, brightness, Wi-Fi, aur torch handle kar sakta hoon."
            )
            return

        # Status
        if cmd in {"status", "system status", "phone status"}:
            b = self.battery_status()
            if b and "percentage" in b:
                self.speak(f"Battery {b.get('percentage')} percent hai. Status {b.get('status', 'unknown')}.")
            else:
                self.speak("Battery status nahi mil paya.")
            return

        # Time / date / day
        if re.search(r"time|samay", cmd):
            self.speak(datetime.now().strftime("Abhi time %I:%M %p hai."))
            return

        if re.search(r"date|aaj ki tareekh|today date", cmd):
            self.speak(datetime.now().strftime("Aaj %d %B %Y hai."))
            return

        if re.search(r"day|aaj ka din", cmd):
            self.speak(datetime.now().strftime("Aaj %A hai."))
            return

        # Battery
        if "battery" in cmd or "charge" in cmd:
            b = self.battery_status()
            if b:
                self.speak(f"Battery {b.get('percentage', '?')} percent hai.")
            else:
                self.speak("Battery status nahi mil paya.")
            return

        # Search
        m = re.match(r"^(search|google|look up)\s+(.+)$", cmd)
        if m:
            query = m.group(2).strip()
            self.open_url("https://www.google.com/search?q=" + quote_plus(query))
            self.speak(f"Google par {query} khol diya.")
            return

        # YouTube
        m = re.match(r"^(youtube|yt|play)\s+(.+)$", cmd)
        if m:
            query = m.group(2).strip()
            self.open_url("https://www.youtube.com/results?search_query=" + quote_plus(query))
            self.speak(f"YouTube par {query} dhoondh raha hoon.")
            return

        # Notes
        m = re.match(r"^(note|remember|save note)\s+(.+)$", cmd)
        if m:
            note = m.group(2).strip()
            self.add_note(note)
            self.speak("Note save kar diya.")
            return

        if cmd in {"read notes", "show notes", "notes"}:
            notes = self.load_notes()
            if not notes:
                self.speak("Koi note nahi hai.")
            else:
                self.speak(f"Total {len(notes)} notes hain.")
                for line in notes[-5:]:
                    self.speak(line)
                    time.sleep(0.15)
            return

        if cmd in {"clear notes", "delete notes", "erase notes"}:
            self.ask_confirm("clear notes", {})
            return

        # Reminders
        m = re.match(r"^(remind me|reminder)\s+(?:in\s+)?(.+?)\s+(?:to\s+|that\s+)?(.+)$", cmd)
        if m:
            duration_text = m.group(2).strip()
            reminder_text = m.group(3).strip()
            delta = self.parse_duration(duration_text)
            if not delta:
                self.speak("Time samajh nahi aaya. Example: remind me in 10 minutes to drink water.")
                return
            when = datetime.now() + delta
            self.add_reminder(when, reminder_text)
            self.speak(f"Reminder set ho gaya, {self.format_duration(delta)} baad.")
            return

        if cmd in {"list reminders", "show reminders", "reminders"}:
            reminders = self.load_reminders()
            active = [r for r in reminders if not r.fired]
            if not active:
                self.speak("Koi active reminder nahi hai.")
            else:
                self.speak(f"Total {len(active)} active reminders hain.")
                for r in active[-5:]:
                    self.speak(f"{r.id}: {r.text}")
            return

        m = re.match(r"^(delete reminder|remove reminder)\s+(\d+)$", cmd)
        if m:
            rid = int(m.group(2))
            self.delete_reminder(rid)
            self.speak(f"Reminder {rid} delete kar diya.")
            return

        # Open / close apps and websites
        m = re.match(r"^(open|launch)\s+(.+)$", cmd)
        if m:
            target = m.group(2).strip()
            self.handle_open(target)
            return

        m = re.match(r"^(close|force close)\s+(.+)$", cmd)
        if m:
            target = m.group(2).strip()
            self.handle_close(target)
            return

        # Call
        m = re.match(r"^(call|dial)\s+(.+)$", cmd)
        if m:
            target = m.group(2).strip()
            self.handle_call(target)
            return

        # SMS
        m = re.match(r"^(sms|message|msg)\s+(.+?)\s+(.*)$", cmd)
        if m:
            target = m.group(2).strip()
            message = m.group(3).strip()
            self.handle_sms(target, message)
            return

        # Wi-Fi / torch
        if cmd in {"wifi on", "turn wifi on", "enable wifi"}:
            self.ask_confirm("wifi on", {})
            return
        if cmd in {"wifi off", "turn wifi off", "disable wifi"}:
            self.ask_confirm("wifi off", {})
            return

        if cmd in {"flashlight on", "torch on", "flash on"}:
            self.ask_confirm("flashlight on", {})
            return
        if cmd in {"flashlight off", "torch off", "flash off"}:
            self.ask_confirm("flashlight off", {})
            return

        # Volume / brightness
        m = re.match(r"^(volume|sound)\s+(?:set\s+)?(\d{1,2})$", cmd)
        if m:
            lvl = int(m.group(2))
            self.volume_set("music", lvl)
            self.speak(f"Volume {lvl} set kar diya.")
            return

        if cmd in {"volume up", "sound up", "increase volume"}:
            self.adjust_volume(+2)
            return
        if cmd in {"volume down", "sound down", "decrease volume"}:
            self.adjust_volume(-2)
            return

        m = re.match(r"^(brightness|screen brightness)\s+(?:set\s+)?(\d{1,3})$", cmd)
        if m:
            lvl = int(m.group(2))
            self.brightness_set(lvl)
            self.speak(f"Brightness {lvl} set kar di.")
            return

        # Contact management
        m = re.match(r"^(add contact|save contact)\s+(.+?)\s+([+\d][\d\s-]+)$", cmd)
        if m:
            name = m.group(2).strip()
            number = self.extract_number(m.group(3))
            self.save_contact(name, number)
            self.speak(f"Contact {name} save kar diya.")
            return

        if cmd in {"list contacts", "contacts"}:
            self.list_contacts()
            return

        # Utilities
        if cmd in {"time now", "what time", "tell time"}:
            self.speak(datetime.now().strftime("Abhi time %I:%M %p hai."))
            return

        if cmd in {"date now", "tell date"}:
            self.speak(datetime.now().strftime("Aaj %d %B %Y hai."))
            return

        if cmd in {"repeat after me"}:
            self.speak("Main sun raha hoon, lekin repeat after me ke liye exact phrase bolo.")
            return

        self.speak("Ye command mere current mode mein nahi hai. Help bolkar commands dekh lo.")

    def handle_open(self, target: str) -> None:
        target = target.strip()
        normalized = self.normalize(target)

        if normalized in APP_MAP:
            self.open_app(APP_MAP[normalized])
            self.speak(f"{target} open kar raha hoon.")
            return

        # Website without scheme -> search or direct open
        if re.match(r"^[a-zA-Z]+://", target):
            self.open_url(target)
            self.speak("Open kar diya.")
            return

        # Common web shortcuts
        web_shortcuts = {
            "google": "https://www.google.com",
            "youtube": "https://www.youtube.com",
            "gmail": "https://mail.google.com",
            "github": "https://github.com",
            "chatgpt": "https://chatgpt.com",
        }
        if normalized in web_shortcuts:
            self.open_url(web_shortcuts[normalized])
            self.speak(f"{target} khol diya.")
            return

        # Try package name or direct URL, otherwise search the web.
        if "." in target and " " not in target:
            url = target if target.startswith("http") else "https://" + target
            self.open_url(url)
            self.speak("Website khol diya.")
            return

        self.open_url("https://www.google.com/search?q=" + quote_plus(target))
        self.speak(f"{target} search kar raha hoon.")

    def handle_close(self, target: str) -> None:
        normalized = self.normalize(target)
        if normalized in APP_MAP:
            self.ask_confirm("force close", {"package": APP_MAP[normalized], "label": target})
            return
        self.speak("App close karne ke liye exact app name bolo ya open app map mein add karo.")

    def handle_call(self, target: str) -> None:
        number = self.resolve_contact(target)
        label = target
        if not number:
            number = self.extract_number(target)
        if not number:
            self.speak("Number ya contact samajh nahi aaya.")
            return
        self.ask_confirm("call", {"number": number, "label": label})

    def handle_sms(self, target: str, message: str) -> None:
        number = self.resolve_contact(target)
        label = target
        if not number:
            number = self.extract_number(target)
        if not number:
            self.speak("SMS ke liye number ya contact nahi mila.")
            return
        if not message:
            self.speak("Message missing hai.")
            return
        self.ask_confirm("sms", {"number": number, "message": message, "label": label})

    def save_contact(self, name: str, number: str) -> None:
        contacts = self.load_contacts()
        contacts[name] = number
        self.save_contacts(contacts)

    def list_contacts(self) -> None:
        contacts = self.load_contacts()
        if not contacts:
            self.speak("Koi saved contact nahi hai.")
            return
        self.speak(f"Total {len(contacts)} saved contacts hain.")
        for name, number in list(contacts.items())[-8:]:
            self.speak(f"{name}: {number}")

    def adjust_volume(self, step: int) -> None:
        # We cannot reliably read the current volume everywhere, so use a conservative guess.
        # Termux uses 0-15 scale for the music stream.
        self.volume_set("music", 7 + step)
        if step > 0:
            self.speak("Volume badha diya.")
        else:
            self.speak("Volume ghata diya.")

    def add_reminder(self, when: datetime, text: str) -> None:
        reminders = self.load_reminders()
        next_id = (max((r.id for r in reminders), default=0) + 1) if reminders else 1
        reminders.append(Reminder(id=next_id, when=when.isoformat(), text=text, fired=False))
        self.save_reminders(reminders)

    def delete_reminder(self, reminder_id: int) -> None:
        reminders = self.load_reminders()
        reminders = [r for r in reminders if r.id != reminder_id]
        self.save_reminders(reminders)

    def format_duration(self, delta: timedelta) -> str:
        total = int(delta.total_seconds())
        parts = []
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        if days:
            parts.append(f"{days} din")
        if hours:
            parts.append(f"{hours} ghante")
        if minutes:
            parts.append(f"{minutes} minute")
        if seconds and not parts:
            parts.append(f"{seconds} second")
        return " ".join(parts) if parts else "thodi der"

    # ---------- reminder worker ----------
    def reminder_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                reminders = self.load_reminders()
                changed = False
                now = datetime.now()
                for reminder in reminders:
                    if reminder.fired:
                        continue
                    if reminder.when_dt <= now:
                        reminder.fired = True
                        changed = True
                        msg = f"Reminder: {reminder.text}"
                        self.reminder_queue.put(msg)
                        self.notify(APP_NAME, reminder.text)
                if changed:
                    self.save_reminders(reminders)
            except Exception as exc:
                self.log(f"Reminder worker error: {exc}")
            time.sleep(15)

    def process_reminder_queue(self) -> None:
        while True:
            try:
                msg = self.reminder_queue.get_nowait()
            except queue.Empty:
                break
            self.speak(msg)

    # ---------- lifecycle ----------
    def start(self) -> None:
        self.speak(f"{APP_NAME} online.")
        self.toast(f"{APP_NAME} ready")
        self.notify(APP_NAME, "Online")
        self.reminder_thread = threading.Thread(target=self.reminder_worker, daemon=True)
        self.reminder_thread.start()

        while not self.stop_event.is_set():
            try:
                self.process_reminder_queue()
                self.speak("Boliye.")
                raw = self.listen()
                self.log(f"HEARD: {raw}")
                cmd = self.strip_wake_word(raw)
                if not cmd:
                    self.speak("Mujhe clear command nahi mila.")
                    continue
                self.handle_command(cmd)
            except KeyboardInterrupt:
                self.speak("Stop kar diya.")
                break
            except Exception as exc:
                self.log(f"Error: {exc}")
                self.speak("Kuch error aaya, lekin main chal raha hoon.")

        self.stop_event.set()
        self.process_reminder_queue()


def main() -> int:
    assistant = JarvisUltra()

    def _sigint(_signum, _frame):
        assistant.stop_event.set()

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)
    assistant.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
