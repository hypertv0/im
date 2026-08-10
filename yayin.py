#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Canli yayin cekici - sadece GitHub Actions icin.

Domain numarasi artar (655, 656, ...). En guncel domaini bulur,
tum yayinlarin direkt m3u8 linklerini ceker, sadece GERCEKTEN
video yayinlayan (ts segmentli) kanallari yazar.

Ciktilar:
  yayinlar.m3u            -> toplu liste
  kanallar/<isim>.m3u8    -> her kanal icin ayri dosya
"""
import requests
import re
import sys
import os
import time
import argparse

START_NUM = 655
MAX_NUM_TRIES = 30
STREAM_TEMPLATE = "https://e-aga-m.3289439f1645c102498296d74fc626b9.online/bc2b05d321cb80050c5d035a9daeb26d/-/{}/playlist.m3u8"
TIMEOUT = 12
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def msg(s):
    if not QUIET:
        print(s)

def find_active_domain():
    for n in range(START_NUM, START_NUM + MAX_NUM_TRIES):
        base = f"https://izlemac{n}.sbs"
        try:
            r = requests.get(base + "/", headers=HEADERS, timeout=8)
            if r.status_code == 200 and "izlema" in r.text:
                msg(f"[OK] domain: {base}")
                return base
        except requests.exceptions.RequestException:
            pass
    return None

def clean_title(t):
    t = re.sub(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF]", "", t)
    return re.sub(r"\s+", " ", t).strip()

def collect_links(base):
    """(baslik, url) listesi: maclar once, kanallar sonra."""
    r = requests.get(base + "/", headers=HEADERS, timeout=TIMEOUT)
    html = r.text
    links = []

    # Maclar: /mac-izle/ - title ozelligi temiz baslik verir
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

    # Kanallar: /canli-mac-izle/ - icerikten baslik
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

    matches = [(t, u) for t, u in links if "/mac-izle/" in u]
    channels = [(t, u) for t, u in links if "/canli-mac-izle/" in u]
    return matches + channels

def page_to_stream(page_url):
    """Sayfadaki match-center.php?id=XXXX degeri dogrudan stream ID'sidir."""
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        m = re.search(r"match-center\.php\?id=(\d+)", r.text)
        if not m:
            return None
        return STREAM_TEMPLATE.format(m.group(1))
    except requests.exceptions.RequestException:
        return None

def api_stream(video_id):
    """streamsport365.com/cinema API'sinden gercek video CDN URL'si alir.

    Oynatici JS'i bu API'yi kullaniyor (ID > 10000 kosuluyla, ama API
    her ID icin URL donduruyor). Donen URL token'li (s=, t=) ve zamanla
    olur — workflow 30 dk'da bir tazeledigi icin sorun degil.
    """
    try:
        r = requests.post(
            "https://streamsport365.com/cinema",
            headers={"Content-Type": "application/json", "Accept": "*/*",
                     "User-Agent": HEADERS["User-Agent"]},
            json={"AppId": "5000", "AppVer": "1", "VpcVer": "1.0.12",
                  "Language": "en", "Token": "", "VideoId": int(video_id)},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return None
        return r.json().get("URL")
    except (requests.exceptions.RequestException, ValueError):
        return None

def verify_video(url):
    """Sadece HTTP 200 degil, GERCEK video (ts segmenti) yayinliyor mu bak."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200 or "EXTM3U" not in r.text:
            return False
        # master playlist'ten chunklist linkini bul
        chunk = [l for l in r.text.split("\n") if "chunklist" in l
                 or (l.endswith(".m3u8") and not l.startswith("#"))]
        if not chunk:
            return False
        c = requests.get(chunk[0], headers=HEADERS, timeout=TIMEOUT)
        if c.status_code != 200 or "EXTM3U" not in c.text:
            return False
        segs = [l for l in c.text.split("\n") if l and not l.startswith("#")]
        # gercek video: .ts segmenti var. .png/.jpg = placeholder (yayin yok)
        return any(".ts" in l for l in segs)
    except requests.exceptions.RequestException:
        return False

def safe_name(title):
    """Dosya adi icin guvenli isim: Turkce karakter + bosluk temizligi."""
    t = title.replace("İ", "I").replace("ı", "i").replace("Ş", "S").replace("ş", "s")
    t = t.replace("Ğ", "G").replace("ğ", "g").replace("Ü", "U").replace("ü", "u")
    t = t.replace("Ö", "O").replace("ö", "o").replace("Ç", "C").replace("ç", "c")
    t = re.sub(r'[^\w\-. ]+', "", t)
    return t.strip().replace(" ", "-")

def write_outputs(streams, out_m3u, out_dir):
    """streams: [(baslik, url)]. Toplu m3u + kanallar/ klasoru yazar.

    Yayin yoksa bile bos #EXTM3U yazar — workflow git add'de cokmesin.
    """
    referer = "https://izlemac655.sbs/"
    lines = ["#EXTM3U"]
    for title, url in streams:
        lines.append(f'#EXTINF:-1 tvg-name="{title}" group-title="Yayinlar",{title}')
        lines.append(f"#EXTVLCOPT:http-referrer={referer}")
        lines.append(f"#EXTVLCOPT:http-user-agent={HEADERS['User-Agent']}")
        lines.append(url)
    with open(out_m3u, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    msg(f"[OK] toplu liste: {out_m3u} ({len(streams)} yayin)")

    os.makedirs(out_dir, exist_ok=True)
    # git bos klasoru commit etmez -> .gitkeep yaz (klasor her zaman dursun)
    keep = os.path.join(out_dir, ".gitkeep")
    if not os.path.exists(keep):
        with open(keep, "w", encoding="utf-8") as f:
            f.write("")
    n = 0
    for title, url in streams:
        fname = os.path.join(out_dir, safe_name(title) + ".m3u8")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(f'#EXTM3U\n#EXTINF:-1,{title}\n{url}\n')
        n += 1
    msg(f"[OK] kanallar klasoru: {out_dir} ({n} dosya)")
    return True

def main():
    global QUIET
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="yayinlar.m3u", help="toplu m3u yolu")
    ap.add_argument("--channels-dir", default="kanallar", help="kanal dosyalari klasoru")
    ap.add_argument("--quiet", action="store_true", help="ciktiyi azalt")
    args = ap.parse_args()
    QUIET = args.quiet

    msg("=== canli yayin cekici ===")
    base = find_active_domain()
    if not base:
        msg("[!] Aktif domain bulunamadi.")
        sys.exit(1)

    links = collect_links(base)
    msg(f"[i] {len(links)} yayin bulundu, dogrulanıyor...")

    streams = []
    for i, (title, url) in enumerate(links, 1):
        # 1) yerel ID -> e-aga-m sablonu (token'siz, kalici)
        stream = page_to_stream(url)
        # 2) gercek video yoksa -> streamsport365 API (token'li, guncel)
        if not (stream and verify_video(stream)):
            mid = re.search(r"match-center\.php\?id=(\d+)", requests.get(url, headers=HEADERS, timeout=TIMEOUT).text)
            api_url = api_stream(mid.group(1)) if mid else None
            if api_url and verify_video(api_url):
                stream = api_url
        if stream and verify_video(stream):
            streams.append((title, stream))
            msg(f"  [{i}/{len(links)}] + {title[:45]}")
        else:
            msg(f"  [{i}/{len(links)}] - {title[:45]} (yayin yok/oldu)")
        time.sleep(0.15)

    write_outputs(streams, args.output, args.channels_dir)
    msg("=== Bitti ===")

if __name__ == "__main__":
    main()
