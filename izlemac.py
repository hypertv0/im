#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
izlemac yayın çekici — tek dosya çözüm
- Aktif domaini otomatik bulur (izlemac655.sbs, izlemac656.sbs, ...)
- Ana sayfadaki TÜM kanal + maç yayınlarının direkt .m3u8 linklerini çeker
- /sdcard/Download/izlemac.m3u dosyasına yazar (Televizo'da açılır)
- En yeni maçlar listenin başında olur

Kullanım:  python izlemac.py
"""
import requests
import re
import base64
import sys
import os
import time
import argparse

# ---------------- AYARLAR ----------------
START_NUM = 655          # İlk denenecek domain numarası
MAX_NUM_TRIES = 30       # Kaç domain denenecek (655..684)
OUTPUT = "/sdcard/Download/izlemac.m3u"
QUIET = False
STREAM_TEMPLATE = "https://e-aga-m.3289439f1645c102498296d74fc626b9.online/bc2b05d321cb80050c5d035a9daeb26d/-/{}/playlist.m3u8"
TIMEOUT = 12

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ---------------- DOMAİN BUL ----------------
def msg(s):
    if not QUIET:
        print(s)

def find_active_domain():
    """izlemac{N}.sbs adreslerini dener, ilk çalışanı döndürür."""
    for n in range(START_NUM, START_NUM + MAX_NUM_TRIES):
        base = f"https://izlemac{n}.sbs"
        try:
            r = requests.get(base + "/", headers=HEADERS, timeout=8)
            if r.status_code == 200 and "izlema" in r.text:
                msg(f"[OK] Aktif domain bulundu: {base}")
                return base
            msg(f"[--] {base} -> HTTP {r.status_code}")
        except requests.exceptions.RequestException:
            msg(f"[--] {base} -> bağlantı yok")
    return None

# ---------------- YAYIN LİNKLERİNİ TOPLA ----------------
def clean_title(t):
    t = re.sub(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def collect_links(base):
    """Ana sayfadan tüm kanal ve maç linklerini toplar. (başlık, url) listesi."""
    r = requests.get(base + "/", headers=HEADERS, timeout=TIMEOUT)
    html = r.text
    links = []

    # 1) MAÇLAR: /mac-izle/ (tam yol) — title özniteliği temiz başlık verir
    for m in re.finditer(r'<a[^>]+href="([^"]*/mac-izle/[^"]+)"[^>]*>', html):
        href = m.group(1)
        if not href.startswith("http"):
            continue
        tm = re.search(r'title="([^"]*)"', m.group(0))
        title = clean_title(tm.group(1)) if tm else ""
        if not title:
            inner = html[m.end():]
            end = inner.find("</a>")
            if end == -1:
                continue
            title = clean_title(re.sub(r"<[^>]+>", "", inner[:end]))
        if title and not any(t == title for t, _ in links):
            links.append((title, href))

    # 2) KANALLAR: /canli-mac-izle/ — <a> içeriğindeki düz metin (title SEO metni içerir)
    for m in re.finditer(r'<a[^>]+href="([^"]*/canli-mac-izle/[^"]+)"[^>]*>', html):
        href = m.group(1)
        if not href.startswith("http"):
            continue
        inner = html[m.end():]
        end = inner.find("</a>")
        if end == -1:
            continue
        title = clean_title(re.sub(r"<[^>]+>", "", inner[:end]))
        if title and not any(t == title for t, _ in links):
            links.append((title, href))

    # Sırala: maçlar önce (yeni yayınlar), kanallar sonra
    matches = [(t, u) for t, u in links if "/mac-izle/" in u]
    channels = [(t, u) for t, u in links if "/canli-mac-izle/" in u]
    return matches + channels

# ---------------- LİNKTEN YAYIN LINKİ ÜRET ----------------
def page_to_stream(page_url, base):
    """Maç/kanal sayfasından direkt m3u8 linki üretir."""
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        m = re.search(r"match-center\.php\?id=(\d+)", r.text)
        if not m:
            return None
        match_id = m.group(1)

        # match-center.php'den mainSource'u çöz
        mc_url = base + "/wp-content/themes/ikisifirbirdokuz/match-center.php?id=" + match_id
        r2 = requests.get(mc_url, headers=HEADERS, timeout=TIMEOUT)
        if r2.status_code != 200:
            return None
        html = r2.text
        ms_pos = html.find("window.mainSource=")
        if ms_pos == -1:
            return None
        seg = html[max(0, ms_pos - 250):ms_pos]
        atobs = list(re.finditer(r'atob.{0,5}\("([A-Za-z0-9+/=]+)"\)', seg))
        if not atobs:
            return None
        b64 = atobs[-1].group(1)
        ms = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", "replace")
        if not ms or ms == "null":
            return None
        return STREAM_TEMPLATE.format(ms)
    except requests.exceptions.RequestException:
        return None

# ---------------- DOĞRULAMA ----------------
def verify_stream(url):
    """Link gerçekten çalışıyor mu? (HTTP 200 + m3u8 içerik)"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        return r.status_code == 200 and "EXTM3U" in r.text
    except requests.exceptions.RequestException:
        return False

# ---------------- M3U YAZ ----------------
def write_m3u(streams):
    """streams: [(başlık, url)] — m3u dosyasına yazar."""
    if not streams:
        print("[!] Hiç yayın bulunamadı, dosya yazılmadı.")
        return False
    lines = ["#EXTM3U"]
    for title, url in streams:
        lines.append(f'#EXTINF:-1 tvg-name="{title}",{title}')
        lines.append(url)
    content = "\n".join(lines) + "\n"

    # /sdcard erişimi yoksa home'a yaz
    target = OUTPUT
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    except (PermissionError, OSError):
        target = os.path.expanduser("~/izlemac.m3u")
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    print(f"[OK] M3U yazıldı: {target} ({len(streams)} yayın)")
    return True

# ---------------- ANA ----------------
def main():
    global OUTPUT, QUIET
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=OUTPUT, help="m3u çıktı yolu")
    ap.add_argument("--quiet", action="store_true", help="çıktıyı azalt")
    args = ap.parse_args()
    OUTPUT = args.output
    QUIET = args.quiet

    msg("=== izlemac yayın çekici ===")
    base = find_active_domain()
    if not base:
        msg("[!] Aktif domain bulunamadı. Site tamamen kapalı olabilir.")
        sys.exit(1)

    links = collect_links(base)
    msg(f"[i] Toplam {len(links)} yayın bulundu, linkleri çekiliyor...")

    streams = []
    for i, (title, url) in enumerate(links, 1):
        stream = page_to_stream(url, base)
        if stream and verify_stream(stream):
            streams.append((title, stream))
            msg(f"  [{i}/{len(links)}] ✓ {title[:45]}")
        else:
            msg(f"  [{i}/{len(links)}] ✗ {title[:45]}")
        time.sleep(0.2)  # nazik olalım

    write_m3u(streams)
    msg("=== Bitti ===")

if __name__ == "__main__":
    main()
