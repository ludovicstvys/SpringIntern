from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import csv
import smtplib
from email.message import EmailMessage
import os
import json

TRACKR_URL = "https://app.the-trackr.com/uk-finance/spring-weeks"

def scrape_open_spring_weeks():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"),
            locale="fr-FR",
        )
        page = context.new_page()

        collected = []

        def handle_resp(resp):
            # On garde toutes les réponses JSON, pas seulement celles qui contiennent "internships"
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if "json" not in ct:
                    return
                data = resp.json()
            except Exception:
                return

            lst = []
            if isinstance(data, dict):
                # Clés fréquemment utilisées
                for key in ("vacancies", "internships", "results", "items"):
                    v = data.get(key)
                    if isinstance(v, list):
                        lst = v
                        break
                # GraphQL/data imbriquée
                if not lst and "data" in data and isinstance(data["data"], dict):
                    for v in data["data"].values():
                        if isinstance(v, list) and v and isinstance(v[0], dict):
                            lst = v
                            break
            elif isinstance(data, list):
                lst = data

            if lst:
                collected.extend(x for x in lst if isinstance(x, dict))

        page.on("response", handle_resp)

        # Charger la page (on ne met pas networkidle trop tôt pour laisser les XHR partir)
        page.goto(TRACKR_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2000)  # laisser partir les premières requêtes

        # Scroll infini jusqu’à stagnation
        stagnant = 0
        seen = 0
        while True:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
            now = len(collected)
            if now == seen:
                stagnant += 1
            else:
                stagnant = 0
                seen = now
            if stagnant >= 3:
                break

        # Fallback: lire le payload Next.js si dispo
        try:
            json_text = page.locator("script#__NEXT_DATA__").inner_text(timeout=2000)
            payload = json.loads(json_text)

            def walk(obj):
                out = []
                if isinstance(obj, dict):
                    for v in obj.values():
                        out.extend(walk(v))
                elif isinstance(obj, list):
                    for v in obj:
                        out.extend(walk(v))
                # On reconnait une "liste d'offres" grossièrement
                if (isinstance(obj, list) and obj and isinstance(obj[0], dict)
                        and {"name", "url"} & set(obj[0].keys())):
                    out.extend(obj)
                return out

            fallback = walk(payload)
            if fallback:
                collected.extend(d for d in fallback if isinstance(d, dict))
        except Exception:
            pass

        browser.close()

    # Dédupliquer par URL ou (company|title)
    uniq = {}
    for d in (x for x in collected if isinstance(x, dict)):
        url = (d.get("url") or d.get("applyUrl") or d.get("link") or "").strip()
        title = (d.get("name") or d.get("title") or "").strip()
        company = ""
        compo = d.get("company") or {}
        if isinstance(compo, dict):
            company = (compo.get("name") or compo.get("title") or "").strip()
        elif isinstance(compo, str):
            company = compo.strip()
        key = url or (company + "|" + title)
        if key and key not in uniq:
            uniq[key] = d

    # Ne garder que les offres avec une date d'ouverture
    results = []
    for d in uniq.values():
        opening = d.get("openingDate") or d.get("openDate") or d.get("opening_date")
        if opening is None:
            continue
        company = ""
        compo = d.get("company") or {}
        if isinstance(compo, dict):
            company = (compo.get("name") or compo.get("title") or "").strip()
        elif isinstance(compo, str):
            company = compo.strip()
        title = (d.get("name") or d.get("title") or "").strip()
        category = (d.get("category") or d.get("programmeType") or "").strip()
        url = (d.get("url") or d.get("applyUrl") or d.get("link") or "").strip()
        results.append((company, title, category, url))
    return results


def read_process_csv(csv_path):
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, mode="r", encoding="utf-8", newline="") as f:
        return [row for row in csv.DictReader(f)]


def ecriture_csv(open_offers, output_file="processus_ouverts.csv"):
    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Company", "Title", "Category", "Url"])
        writer.writerows(open_offers)
    print(f"{len(open_offers)} offres exportées dans : {output_file}")
    return output_file


def send_email(open_offers, old_procs, mailing_list, csv_path=None):
    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER   = os.getenv("SMTP_USER")
    SMTP_PASS   = os.getenv("SMTP_PASS_APP")

    if not (SMTP_USER and SMTP_PASS):
        raise RuntimeError("Identifiants SMTP manquants (SMTP_USER / SMTP_PASS_APP).")

    to_addrs  = [row['email'].strip() for row in mailing_list if row.get('email')]

    body = "Voici la liste des nouveaux Spring internships:\n\n" + \
           "\n".join(f"• {c} – {t} - {cat} - {u}" for c, t, cat, u in open_offers)
    body += "\n\nVoici la liste des Spring internships qui sont déjà ouverts:\n\n" + \
            "\n".join(f"• {c} – {t} - {cat} - {u}" for c, t, cat, u in old_procs)

    msg = EmailMessage()
    msg["Subject"] = "Nouveaux Spring ouverts"
    msg["From"]    = SMTP_USER
    msg["To"]      = ", ".join(to_addrs)
    msg.set_content(body)

    if csv_path:
        with open(csv_path, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="text", subtype="csv", filename=os.path.basename(csv_path))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.set_debuglevel(1)
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)

    print(f"Email envoyé à : {to_addrs}")


def new_process(offres, process_rows):
    """
    Compare la liste 'offres' avec le CSV des process (colonnes: Company, ...)
    """
    companies = { (row.get("Company") or "").strip() for row in process_rows }
    new_procs, old_procs = [], []
    for comp, title, category, url in offres:
        if comp in companies:
            old_procs.append((comp, title, category, url))
        else:
            new_procs.append((comp, title, category, url))
    return new_procs, old_procs


if __name__ == "__main__":
    offres = scrape_open_spring_weeks()
    procs  = read_process_csv("processus_ouverts.csv")
    newprocs, oldprocs = new_process(offres, procs)

    # Toujours réécrire le CSV avec l'état courant
    csv_file = ecriture_csv(offres)

    if len(newprocs) > 0:
        mailing = read_process_csv("email.csv")
        send_email(newprocs, oldprocs, mailing, csv_path=csv_file)
    else:
        print("Aucune nouvelle offre ouverte détectée.")
