import os, sys, json, re, html, time, random
import requests
from DrissionPage import ChromiumPage, ChromiumOptions

# --- AYARLAR (anahtarlar GitHub Secrets'tan gelir) ---
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
ARAMA_LINKLERI = [l.strip() for l in os.environ["SEARCH_URLS"].splitlines() if l.strip()]
TEST_MODU = os.environ.get("TEST_MODU", "false").lower() == "true"

FILTRE_AKTIF = True   # False yaparsan hasarlı ilanlar da gelir
MAX_ILAN = 5          # her çalışmada en fazla kaç yeni ilan incelensin
SEEN_FILE = "seen.json"

# 0 = sadece orijinal, 1 = lokal boyalı olabilir, 2 = fark etmez
PARCA_AYARLARI = {
    "tavan": 0, "kaput": 0, "bagaj": 1,
    "sol ön çamurluk": 2, "sağ ön çamurluk": 2,
    "sol arka çamurluk": 2, "sağ arka çamurluk": 2,
    "sol ön kapı": 2, "sağ ön kapı": 2,
    "sol arka kapı": 2, "sağ arka kapı": 2,
}


def telegram_gonder(mesaj):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": mesaj[:4000], "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30)
        if r.status_code != 200:
            print("[TELEGRAM HATA]", r.text)
    except Exception as e:
        print("[TELEGRAM HATA]", e)


# --- TARAYICI ---
def connect_browser():
    co = ChromiumOptions()
    co.set_browser_path("/usr/bin/google-chrome")
    co.auto_port()
    co.set_argument("--headless=new")
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--window-size=1366,900")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    return ChromiumPage(co)


def wait_until_loaded(tab):
    for _ in range(30):
        try:
            if tab.run_js("return document.readyState;") == "complete":
                break
        except Exception:
            pass
        time.sleep(0.5)
    time.sleep(random.uniform(1.5, 2.5))


# --- İLAN SAYFASINDAN VERİ ÇEKME ---
def extract_vehicle_info(tab, log):
    yil, km = "-", "-"
    try:
        rows = tab.run_js(
            'return [...document.querySelectorAll("dt, dd, dl *, ul.classifiedInfoList li *")]'
            '.map(e => e.innerText.trim());')
        if rows and isinstance(rows, list):
            for i, val in enumerate(rows):
                if "Kilometre" in val and i + 1 < len(rows) and km == "-":
                    km = rows[i + 1]
                if "Yıl" in val and i + 1 < len(rows) and yil == "-":
                    yil = rows[i + 1]
        if km == "-" or yil == "-":
            full = tab.run_js("return document.body.innerText;")
            m_km = re.search(r'(?:Kilometre|KM)\s*[:\n]\s*([\d.]+)', full, re.I)
            m_yil = re.search(r'(?:Yıl|Model)\s*[:\n]\s*(\d{4})', full, re.I)
            if m_km and km == "-": km = m_km.group(1)
            if m_yil and yil == "-": yil = m_yil.group(1)
    except Exception:
        pass
    return yil, km


def extract_expertise_list(tab, log):
    try:
        tab.run_js("""
        [...document.querySelectorAll('a, li, span, div')].filter(el => {
            let t = (el.innerText || "").trim().toLowerCase();
            return t.includes("boya - değişen") || t.includes("ekspertiz");
        }).forEach(t => { try { t.click(); } catch(e){} });
        """)
        time.sleep(2.5)
        data = tab.run_js("""
        let data = [];
        for (let ul of document.querySelectorAll('ul')) {
            let titleNode = ul.querySelector('li.pair-title');
            if (titleNode) {
                let status = titleNode.innerText.trim();
                ul.querySelectorAll('li.selected-damage').forEach(p => {
                    data.push({durum: status, parca: p.innerText.trim()});
                });
            }
        }
        return data;
        """)
        if data:
            log.append(f"Ekspertiz listesinde {len(data)} hasarlı parça bulundu.")
            return json.dumps(data, ensure_ascii=False)
        log.append("Ekspertiz listesi boş (araç hatasız olabilir veya sekme açılmadı).")
    except Exception as e:
        log.append(f"Liste okuma hatası: {e}")
    return "[]"


def extract_description(tab):
    try:
        el = tab.ele("css:#classifiedDescription", timeout=2)
        if el:
            return el.text.strip()
    except Exception:
        pass
    return ""


# --- ANALİZ (yapay zekasız) ---
def _parca_bul(metin):
    m = metin.lower()
    m = m.replace("motor kaputu", "kaput").replace("bagaj kapağı", "bagaj")
    for parca in PARCA_AYARLARI:
        if parca in m:
            return parca
    for parca in ("tavan", "kaput", "bagaj"):
        if parca in m:
            return parca
    return None


def analyze_list(json_string):
    sonuc = {}
    try:
        for x in json.loads(json_string):
            parca = _parca_bul(x["parca"])
            d = x["durum"].lower()
            if "değişen" in d:
                seviye = 3
            elif "lokal" in d:
                seviye = 1
            elif "boyalı" in d or "boyali" in d:
                seviye = 2
            else:
                continue
            if parca:
                sonuc[parca] = max(sonuc.get(parca, 0), seviye)
    except Exception:
        pass
    return sonuc


ANAHTAR = ("boyalı", "boyali", "boya", "değişen", "degisen", "değişmiş",
           "lokal", "çizik", "ezik", "hasar", "tramer", "kaza")


def analyze_description(metin):
    """Açıklamada hasar ifadesi geçen cümleleri döndürür."""
    if not metin:
        return []
    cumleler = re.split(r"[.\n!?]+", metin)
    return [c.strip() for c in cumleler
            if any(k in c.lower() for k in ANAHTAR)
            and "hasarsız" not in c.lower()
            and "boyasız" not in c.lower()
            and "değişensiz" not in c.lower()][:5]


# --- RAPOR ---
def send_report(ilan, yil, km, nihai, supheli, log, ham_json):
    temiz_mi = True
    lokal, boyali, degisen = [], [], []
    for parca, durum in nihai.items():
        ayar = PARCA_AYARLARI.get(parca, 2)
        if durum == 1:
            lokal.append(parca.title())
            if ayar == 0: temiz_mi = False
        elif durum == 2:
            boyali.append(parca.title())
            if ayar < 2: temiz_mi = False
        elif durum == 3:
            degisen.append(parca.title())
            if ayar < 2: temiz_mi = False

    if not TEST_MODU and FILTRE_AKTIF and not temiz_mi:
        print(f"[FİLTRE] {ilan['id']} ayarlara uymadığı için gönderilmedi.")
        return

    satirlar = []
    if degisen: satirlar.append(f"🔴 <b>Değişen ({len(degisen)}):</b> {', '.join(degisen)}")
    if boyali: satirlar.append(f"🟠 <b>Boyalı ({len(boyali)}):</b> {', '.join(boyali)}")
    if lokal: satirlar.append(f"🟡 <b>Lokal Boyalı ({len(lokal)}):</b> {', '.join(lokal)}")
    rapor = "\n".join(satirlar) if satirlar else "🟢 <b>Listede hasar görünmüyor</b>"

    if supheli:
        supheli_str = html.escape("\n".join("• " + c[:200] for c in supheli))
    else:
        supheli_str = "✅ Açıklamada hasar ifadesi yok"

    ham = "Araç kusursuz veya liste açılmadı."
    if ham_json != "[]":
        try:
            ham = "\n".join(f"▪️ {html.escape(x['parca'])}: <b>{html.escape(x['durum'])}</b>"
                            for x in json.loads(ham_json))
        except Exception:
            ham = html.escape(ham_json)

    baslik = "🧪 <b>TEST İLANI</b>" if TEST_MODU else "🚨 <b>YENİ İLAN!</b>"
    mesaj = (
        f"{baslik}\n\n📌 <b>{html.escape(ilan['baslik'])}</b>\n"
        f"💰 <b>Fiyat:</b> {html.escape(ilan['fiyat'])}\n"
        f"📅 <b>Yıl:</b> {html.escape(str(yil))} | 🛣️ <b>KM:</b> {html.escape(str(km))}\n\n"
        f"🚘 <b>HASAR DURUMU:</b>\n{rapor}\n\n"
        f"📝 <b>Açıklamada geçenler:</b>\n{supheli_str}\n\n"
        f"🔬 <b>Siteden çekilen liste:</b>\n{ham}\n\n"
        f"🔗 <a href='{ilan['link']}'>İlana Git</a>"
    )
    telegram_gonder(mesaj)


def scan_listing(page, ilan):
    log = []
    tab = page.new_tab(ilan["link"])
    try:
        wait_until_loaded(tab)
        yil, km = extract_vehicle_info(tab, log)
        ham_json = extract_expertise_list(tab, log)
        aciklama = extract_description(tab)
    finally:
        tab.close()
    nihai = analyze_list(ham_json)
    supheli = analyze_description(aciklama)
    send_report(ilan, yil, km, nihai, supheli, log, ham_json)


# --- ANA AKIŞ ---
def main():
    ilk_kurulum = not os.path.exists(SEEN_FILE)
    seen = set(json.load(open(SEEN_FILE))) if not ilk_kurulum else set()

    page = connect_browser()
    islenen = 0
    hata_var = False

    for link in ARAMA_LINKLERI:
        page.get(link)
        wait_until_loaded(page)
        ilanlar = page.eles("css:tr.searchResultsItem")
        if not ilanlar:
            print("[HATA] İlan bulunamadı. Sayfa başlığı:", page.title)
            print("Muhtemelen sahibinden GitHub IP'sini engelledi.")
            hata_var = True
            continue

        for el in ilanlar:
            try:
                ilan_id = el.attr("data-id")
                if not ilan_id:
                    continue
                if ilan_id in seen and not TEST_MODU:
                    continue
                ilan = {
                    "id": ilan_id,
                    "baslik": el.ele("css:a.classifiedTitle").text,
                    "fiyat": el.ele("css:td.searchResultsPriceValue").text,
                    "link": el.ele("css:a.classifiedTitle").attr("href"),
                }
            except Exception:
                continue

            if ilk_kurulum and not TEST_MODU:
                seen.add(ilan_id)   # ilk çalışmada sessizce hafızaya al
                continue

            if islenen >= MAX_ILAN:
                break  # kalanlar bir sonraki çalışmada işlenir

            try:
                scan_listing(page, ilan)
                if not TEST_MODU:
                    seen.add(ilan_id)
            except Exception as e:
                print(f"[HATA] {ilan_id}: {e}")
            islenen += 1
            if TEST_MODU:
                break
            time.sleep(random.uniform(5, 10))
        time.sleep(random.uniform(5, 10))

    if not TEST_MODU:
        json.dump(sorted(seen), open(SEEN_FILE, "w"))
    try:
        page.quit()
    except Exception:
        pass
    if hata_var:
        sys.exit(1)


main()
