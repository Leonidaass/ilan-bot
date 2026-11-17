import os, sys, json, re, html, time, random
import requests
from DrissionPage import ChromiumPage, ChromiumOptions
from google import genai

# --- AYARLAR (anahtarlar GitHub Secrets'tan gelir) ---
GEMINI_KEY = os.environ["GEMINI_KEY"]
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
ARAMA_LINKLERI = [l.strip() for l in os.environ["SEARCH_URLS"].splitlines() if l.strip()]
TEST_MODU = os.environ.get("TEST_MODU", "false").lower() == "true"

FILTRE_AKTIF = True   # False yaparsan hasarlı ilanlar da gelir
MAX_ILAN = 5          # her çalışmada en fazla kaç yeni ilan incelensin
SEEN_FILE = "seen.json"

# 0 = sadece orijinal, 1 = boyalı/lokal olabilir, 2 = fark etmez
PARCA_AYARLARI = {
    "tavan": 0, "kaput": 0, "bagaj": 1,
    "sol ön çamurluk": 2, "sağ ön çamurluk": 2,
    "sol arka çamurluk": 2, "sağ arka çamurluk": 2,
    "sol ön kapı": 2, "sağ ön kapı": 2,
    "sol arka kapı": 2, "sağ arka kapı": 2,
}

ai_client = genai.Client(api_key=GEMINI_KEY)


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
    log.append("KM/Yıl DOM üzerinden okundu.")
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


# --- GEMINI ---
def parse_gemini_response(text):
    try:
        temiz = text.replace("```json", "").replace("```", "").strip()
        m = re.search(r"\{.*?\}", temiz, re.DOTALL)
        if m:
            return {k.lower().strip(): int(v) for k, v in json.loads(m.group(0)).items()}
    except Exception:
        pass
    return {}


SABLON = ('{"tavan":0, "kaput":0, "bagaj":0, "sol ön çamurluk":0, "sağ ön çamurluk":0, '
          '"sol arka çamurluk":0, "sağ arka çamurluk":0, "sol ön kapı":0, "sağ ön kapı":0, '
          '"sol arka kapı":0, "sağ arka kapı":0}')


def gemini_sor(prompt):
    try:
        r = ai_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return parse_gemini_response(r.text)
    except Exception as e:
        print("[GEMINI HATA]", e)
        return {}


def analyze_list(json_string):
    if json_string == "[]":
        return {}
    return gemini_sor(
        "Aşağıda bir araba ilanının ekspertiz panelinden çekilmiş ham liste verisi var.\n"
        "Kurallar: Orijinal / Listede olmayan = 0, Lokal Boyalı = 1, Boyalı = 2, Değişen = 3. "
        "'Motor Kaputu' = 'kaput'\n"
        f"Sadece şu formatta JSON döndür:\n{SABLON}\nHAM LİSTE: {json_string}")


def analyze_description(metin):
    if not metin:
        return {}
    return gemini_sor(
        "Aşağıdaki araç ilan açıklamasını oku. Satıcının metinde itiraf ettiği hasarları bul.\n"
        "Kurallar: Orijinal = 0, Lokal Boyalı = 1, Boyalı = 2, Değişen = 3.\n"
        f"Sadece şu JSON formatını döndür:\n{SABLON}\nAçıklama: {metin}")


def cross_check(sema, aciklama, log):
    durum_str = {0: "Orijinal", 1: "Lokal Boyalı", 2: "Boyalı", 3: "Değişen"}
    nihai, celiskiler = {}, []
    for parca in PARCA_AYARLARI:
        s, a = sema.get(parca, 0), aciklama.get(parca, 0)
        if s != a:
            celiskiler.append(f"⚠ {parca.title()}: Liste={durum_str.get(s, '?')} | Açıklama={durum_str.get(a, '?')}")
            nihai[parca] = max(s, a)
        else:
            nihai[parca] = s
    if celiskiler:
        log.append("Açıklama ile ekspertiz listesi uyuşmuyor!")
    return nihai, celiskiler


# --- RAPOR ---
def send_report(ilan, yil, km, nihai, celiskiler, log, ham_json):
    temiz_mi = True
    lokal, boyali, degisen = [], [], []
    for parca, durum in nihai.items():
        ayar = PARCA_AYARLARI.get(parca, 2)
        if durum == 1:
            lokal.append(parca.title())
            if ayar == 0: temiz_mi = False
        elif durum == 2:
            boyali.append(parca.title())
            if ayar == 0: temiz_mi = False
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
    rapor = "\n".join(satirlar) if satirlar else "🟢 <b>Tüm Parçalar Orijinal / Hatasız</b>"
    celiski_str = html.escape("\n".join(celiskiler)) if celiskiler else "✅ Çelişki yok"

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
        f"📊 <b>Uyuşmazlık:</b>\n{celiski_str}\n\n"
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
    sema = analyze_list(ham_json)
    ai_acik = analyze_description(aciklama)
    nihai, celiskiler = cross_check(sema, ai_acik, log)
    send_report(ilan, yil, km, nihai, celiskiler, log, ham_json)


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
