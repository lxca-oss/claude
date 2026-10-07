"""Build business notes data (JSON) from pandoc HTML of Luca's notes, ordered by the HSC syllabus."""
import html, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hr_strat import HR_STRAT, MN_FIX

SRC = sys.argv[1]   # dir with Finance.html etc.
OUT = sys.argv[2]

# Syllabus structure. Each point: (text, mnemonic, [sub-points], [note headings], extra)
# extra may contain {"related": [...]} for points with no notes.
SYL = [
 ("finance", "Finance", "Finance", [
  ("R", "Role", [
   ("strategic role of financial management", "PEGSL, PEGL, PEGS", [], ["Strategic Role of Financial Management"]),
   ("objectives of financial management", "", ["profitability, growth, efficiency, liquidity, solvency", "short-term and long-term"], ["Objectives of Financial Management (PEGSL)"]),
   ("interdependence with other key business functions", "", [], ["Interdependence with Other KBFs"]),
  ]),
  ("I", "Influences", [
   ("internal sources of finance – retained profits", "", [], ["Internal Sources of Finance"]),
   ("external sources of finance", "COF, MUDL, NSPR, PE", ["debt – short-term borrowing (overdraft, commercial bills, factoring), long-term borrowing (mortgage, debentures, unsecured notes, leasing)", "equity – ordinary shares (new issues, rights issues, placements, share purchase plans), private equity"], ["External Sources of Finance"]),
   ("financial institutions – banks, investment banks, finance companies, superannuation funds, life insurance companies, unit trusts and the Australian Securities Exchange", "BUSFAIL", [], ["Financial Institutions (BUSFAIL)"]),
   ("influence of government – Australian Securities and Investment Commission, company taxation", "AC", [], ["Influence of Government (ASIC and Company Tax)"]),
   ("global market influences – economic outlook, availability of funds, interest rates", "IEA", [], ["Global Market Influences (IGA)"]),
  ]),
  ("P", "Processes", [
   ("planning and implementing – financial needs, budgets, record systems, financial risks, financial controls", "FOP", ["debt and equity financing – advantages and disadvantages of each", "matching the terms and source of finance to business purpose"], ["Planning and Implementing (BRF3)"]),
   ("monitoring and controlling – cash flow statement, income statement, balance sheet", "", [], ["Monitoring and Controlling"]),
   ("financial ratios", "WAO", ["liquidity – current ratio (current assets ÷ current liabilities)", "gearing – debt to equity ratio (total liabilities ÷ total equity)", "profitability – gross profit ratio (gross profit ÷ sales); net profit ratio (net profit ÷ sales); return on equity ratio (net profit ÷ total equity)", "efficiency – expense ratio (total expenses ÷ sales), accounts receivable turnover ratio (sales ÷ accounts receivable)", "comparative ratio analysis – over different time periods, against standards, with similar businesses"], ["Financial Ratios", "Comparative Ratio Analysis (LIT)"]),
   ("limitations of financial reports – normalised earnings, capitalising expenses, valuing assets, timing issues, debt repayments, notes to the financial statements", "CNN DTV", [], ["Limitations of Financial Reports (TVCNND)"]),
   ("ethical issues related to financial reports", "", [], ["Ethical Issues Related to Financial Reports (RAR)"]),
  ]),
  ("S", "Strategies", [
   ("cash flow management", "DDF", ["cash flow statements", "distribution of payments, discounts for early payment, factoring"], ["Cash Flow Management (DDF)"]),
   ("working capital management", "RIC, LOP, SL", ["control of current assets – cash, receivables, inventories", "control of current liabilities – payables, loans, overdrafts", "strategies – leasing, sale and lease back"], ["Working Capital Management (CA-CL-L-SL)"]),
   ("profitability management", "FEC", ["cost controls – fixed and variable, cost centres, expense minimisation", "revenue controls – marketing objectives"], ["Profitability Management (SPS)"]),
   ("global financial management", "DHIME", ["exchange rates", "interest rates", "methods of international payment – payment in advance, letter of credit, clean payment, bill of exchange", "hedging", "derivatives"], ["Global Financial Management (FEC, HIMED, PLBC)"]),
  ]),
 ]),
 ("marketing", "Marketing", "Marketing", [
  ("R", "Role", [
   ("strategic role of marketing goods and services", "", [], ["Strategic Role of Marketing"]),
   ("interdependence with other key business functions", "", [], ["Interdependence with Other KBFs"]),
   ("production, selling, marketing approaches", "", [], ["Production, Selling and Marketing Approaches"]),
   ("types of markets – resource, industrial, intermediate, consumer, mass, niche", "CRIMIN", [], ["Types of Markets (CRIMIN)"]),
  ]),
  ("I", "Influences", [
   ("factors influencing customer choice – psychological, sociocultural, economic, government", "PEGS, LAMPP, CRFS", [], ["Factors Influencing Customer Choice (PEGS)"]),
   ("consumer laws", "WIPD", ["deceptive and misleading advertising", "price discrimination", "implied conditions", "warranties"], ["Consumer Laws (WIMP)"]),
   ("ethical – truth, accuracy and good taste in advertising, products that may damage health, engaging in fair competition, sugging", "GTAPES", [], ["Ethical Influences on Marketing (TAPES)"]),
  ]),
  ("P", "Processes", [
   ("situational analysis – SWOT, product life cycle", "SP", [], ["Situational Analysis"]),
   ("market research", "DDD", [], ["Market Research (DDD)"]),
   ("establishing market objectives", "", [], ["Establishing Marketing Objectives"]),
   ("identifying target markets", "", [], ["Identifying Target Markets"]),
   ("developing marketing strategies", "", [], ["Developing Marketing Strategies"]),
   ("implementation, monitoring and controlling – developing a financial forecast; comparing actual and planned results, revising the marketing strategy", "", [], ["Implementation, Monitoring and Controlling"]),
  ]),
  ("S", "Strategies", [
   ("market segmentation, product/service differentiation and positioning", "DGPB", [], ["Market Segmentation (DGPB)", "Product/Service Differentiation", "Positioning"]),
   ("products – goods and/or services", "", ["branding", "packaging"], ["Product: Goods and/or Services"]),
   ("price including pricing methods – cost, market, competition-based", "CMC, PPPL", ["pricing strategies – skimming, penetration, loss leaders, price points", "price and quality interaction"], ["Price (CMC, SLPP)", "Price and Quality Interaction"]),
   ("promotion", "RAPPPS, OLWOM", ["elements of the promotion mix – advertising, personal selling and relationship marketing, sales promotions, publicity and public relations", "the communication process – opinion leaders, word of mouth"], ["Promotion (RAPPPS)"]),
   ("place/distribution", "ISE, WIT", ["distribution channels", "channel choice – intensive, selective, exclusive", "physical distribution issues – transport, warehousing, inventory"], ["Place/Distribution", "Channel Choice (Market Coverage)", "Physical Distribution Issues"]),
   ("people, processes and physical evidence", "", [], ["People, Processes and Physical Evidence"]),
   ("e-marketing", "", [], ["E-Marketing"]),
   ("global marketing", "GCSCG", ["global branding", "standardisation", "customisation", "global pricing", "competitive positioning"], ["Global Marketing"]),
  ]),
 ]),
 ("operations", "Operations", "Operations", [
  ("R", "Role", [
   ("strategic role of operations management – cost leadership, good/service differentiation", "", [], ["Context: Performance Objectives of Operations (CDF CSQ)", "Strategic Role of Operations Management"]),
   ("goods and/or services in different industries", "", [], ["Goods and/or Services in Different Industries"]),
   ("interdependence with other key business functions", "", [], ["Interdependence with Other KBFs"]),
  ]),
  ("I", "Influences", [
   ("globalisation, technology, quality expectations, cost-based competition, government policies, legal regulation, environmental sustainability", "TEGGLQC, RAGGG", [], ["Globalisation (RAGGG)", "Technology", "Quality Expectations", "Cost-based Competition", "Government Policies", "Legal Regulations", "Environmental Sustainability"]),
   ("corporate social responsibility", "", ["the difference between legal compliance and ethical responsibility", "environmental sustainability and social responsibility"], ["Corporate Social Responsibility (CSR)"]),
  ]),
  ("P", "Processes", [
   ("inputs", "MIC, HF", ["transformed resources (materials, information, customers)", "transforming resources (human resources, facilities)"], ["Inputs"]),
   ("transformation processes", "VVVV, FOPP", ["the influence of volume, variety, variation in demand and visibility (customer contact)", "sequencing and scheduling – Gantt charts, critical path analysis", "technology, task design and process layout", "monitoring, control and improvement"], ["Transformation Processes: Influence of the 4Vs (VVVV)", "Sequencing and Scheduling", "Technology, Task Design and Process Layout", "Monitoring, Control and Improvement (MCI)"]),
   ("outputs", "", ["customer service", "warranties"], ["Outputs"]),
  ]),
  ("S", "Strategies", [
   ("performance objectives – quality, speed, dependability, flexibility, customisation, cost", "CDF CSQ", [], ["Performance Objectives (CDF CSQ)"]),
   ("new product or service design and development", "", [], ["New Product/Service Design and Development"]),
   ("supply chain management – logistics, e-commerce, global sourcing", "DTSM, LEG", [], ["Supply Chain Management (LEG, DTSM)"]),
   ("outsourcing – advantages and disadvantages", "", [], ["Outsourcing"]),
   ("technology – leading edge, established", "", [], ["Technology (Leading-edge and Established)"]),
   ("inventory management – advantages and disadvantages of holding stock, LIFO (last-in-first-out), FIFO (first-in-first-out), JIT (just-in-time)", "HS, JIT, FIFO, LIFO", [], ["Inventory Management (LEG)"]),
   ("quality management", "", ["control", "assurance", "improvement"], ["Quality Management"]),
   ("overcoming resistance to change – financial costs, purchasing new equipment, redundancy payments, retraining, reorganising plant layout, inertia", "PIF RRR", [], ["Overcoming Resistance to Change (RIPPP F)"]),
   ("global factors – global sourcing, economies of scale, scanning and learning, research and development", "REGS", [], ["Global Factors (REGS)"]),
  ]),
 ]),
 ("hr", "Human Resources", "HR", [
  ("R", "Role", [
   ("strategic role of human resources", "", [], ["Strategic Role of Human Resources"]),
   ("interdependence with other key business functions", "", [], ["Interdependence with Other Key Business Functions"]),
   ("outsourcing", "", ["human resource functions", "using contractors – domestic, global"], ["Outsourcing"]),
  ]),
  ("I", "Influences", [
   ("stakeholders – employers, employees, employer associations, unions, government organisations, society", "UGEESE", [], ["Stakeholders (U GEESE)"]),
   ("legal – the current legal framework", "LEGJI", ["the employment contract – common law (rights and obligations of employers and employees), minimum employment standards, minimum wage rates, awards, enterprise agreements, other employment contracts", "occupational health and safety and workers compensation", "antidiscrimination and equal employment opportunity"], ["Legal: The Current Legal Framework", "Legal: Occupational Health and Safety and Workers Compensation", "Legal: Antidiscrimination and Equal Employment Opportunity"]),
   ("economic", "", [], ["Economic"]),
   ("technological", "HEAD", [], ["Technological"]),
   ("social – changing work patterns, living standards", "CL", [], ["Social (MICA)"]),
   ("ethics and corporate social responsibility", "", [], ["Ethics and Corporate Social Responsibility"]),
  ]),
  ("P", "Processes", [
   ("acquisition", "IRS", [], ["Acquisition (IRS)"]),
   ("development", "ITOMP", [], ["Development (ITOMP)"]),
   ("maintenance", "FLECR", [], ["Maintenance (FLECM)"]),
   ("separation", "RRRV, CIDR, SUD", [], ["Separation (RRRV, CIDR)"]),
  ]),
  ("S", "Strategies", [
   ("leadership style", "LAD", [], [], {}),
   ("job design – general or specific tasks", "", [], [], {}),
   ("recruitment – internal or external, general or specific skills", "", [], [], {"related": [("P", "Acquisition", "internal vs external recruitment")]}),
   ("training and development – current or future skills", "", [], [], {"related": [("P", "Development", "induction, training, organisational development, mentoring and coaching")]}),
   ("performance management – developmental or administrative", "", [], [], {"related": [("P", "Development", "performance appraisals")]}),
   ("rewards – monetary and non-monetary, individual or group, performance pay", "", [], [], {"related": [("P", "Maintenance", "monetary and non-monetary rewards")]}),
   ("global – costs, skills, supply", "", [], [], {"related": [("R", "Outsourcing", "global contractors")]}),
   ("workplace disputes", "", ["resolution – negotiation, mediation, grievance procedures, involvement of courts and tribunals"], [], {"related": [("I", "Legal", "FWC mediation and conciliation, courts and tribunals, enterprise agreement dispute processes")]}),
  ]),
  ("E", "Effectiveness", [
   ("indicators", "CLAW ABC", ["corporate culture", "benchmarking key variables", "changes in staff turnover", "absenteeism", "accidents", "levels of disputation", "worker satisfaction"], ["__E__"]),
  ]),
 ]),
]

# Mnemonics from Luca's syllabus sheet: point-level overrides and sub-point tags
PMN = {
 ("finance", "external sources of finance"): "", ("finance", "financial ratios"): "", ("finance", "cash flow management"): "",
 ("finance", "working capital management"): "", ("finance", "profitability management"): "",
 ("marketing", "factors influencing customer choice"): "PEGS, LAMPP, CRFS, FMR", ("marketing", "establishing market objectives"): "PIIIEE",
 ("marketing", "implementation, monitoring and controlling"): "SMM, NPC", ("marketing", "price including pricing methods"): "CMC",
 ("marketing", "promotion"): "RAPPPS", ("marketing", "place/distribution"): "",
 ("operations", "globalisation, technology, quality expectations, cost-based competition, government policies, legal regulation, environmental sustainability"): "TEGGLQC, RAGGG, QFD RLP",
 ("operations", "inputs"): "", ("operations", "transformation processes"): "",
}
SUBMN = {
 "debt – short-term": "COF, MUDL", "equity – ordinary": "NSPR, PE", "comparative ratio analysis": "WAO",
 "distribution of payments": "DDF", "control of current assets": "RIC", "control of current liabilities": "LOP", "strategies – leasing": "SL",
 "cost controls": "FEC", "packaging": "ISCAP", "pricing strategies": "PPPL", "elements of the promotion mix": "METDBS",
 "the communication process": "OLWOM", "channel choice": "ISE", "physical distribution": "WIT", "global pricing": "CMC",
 "transformed resources": "MIC", "transforming resources": "HF", "the influence of volume": "VVVV", "technology, task design": "DDD, FOPP",
}

def fix(s):
    for a, b in MN_FIX: s = s.replace(a, b)
    return s

def text(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()

def clean(body):
    body = fix(body)
    body = re.sub(r"<colgroup>.*?</colgroup>\s*", "", body, flags=re.S)
    body = re.sub(r' (class|style|id)="[^"]*"', "", body)
    body = body.replace("<th><strong>", "<th>").replace("</strong></th>", "</th>")
    # Labels: Def / How / Positives / Negatives / Tip / Note
    def lab(m):
        k = m.group(1).strip()
        low = k.lower()
        cls = "def" if low.startswith("def") else "pos" if "positive" in low and "negative" not in low else "neg" if "negative" in low and "positive" not in low else "tip" if low in ("tip", "note") else "how" if low.startswith("how") else "k"
        return f'<span class="lb {cls}">{k}</span>'
    body = re.sub(r"<strong>([^<:]{1,60}):</strong>", lab, body)
    # list items starting with a plain "Label:" (e.g. "Positives:", "Def:")
    def li(m):
        k = m.group(1)
        low = k.lower()
        cls = "def" if low.startswith("def") else "pos" if "positive" in low and "negative" not in low else "neg" if "negative" in low and "positive" not in low else "tip" if low in ("tip", "note") else "how" if low.startswith("how") else None
        if not cls: return m.group(0)
        return f'<li><span class="lb {cls}">{k}</span>'
    body = re.sub(r"<li>(Def|Defs|How(?: \([^)]*\))?|Positives(?: \([^)]*\))?|Negatives(?: \([^)]*\))?|Tip|Note|Features|Why|Effects?|Implications):", li, body)
    # Tip / Note paragraphs become callouts
    body = re.sub(r'<p><span class="lb tip">(Tip|Note)</span>(.*?)</p>', r'<div class="callout"><span class="ic">!</span><div class="ct"><p><b>\1:</b>\2</p></div></div>', body, flags=re.S)
    body = re.sub(r"<table>", '<div class="tbl-wrap"><table class="db">', body)
    body = body.replace("</table>", "</table></div>")
    return body.strip()

def parse(path):
    src = open(path, encoding="utf-8").read()
    parts = re.split(r'(<h[1-4][^>]*>.*?</h[1-4]>)', src, flags=re.S)
    nodes = []  # flat: level, title, body
    pre = parts[0]
    for i in range(1, len(parts), 2):
        lvl = int(parts[i][2]); title = text(parts[i])
        nodes.append({"lvl": lvl, "title": title, "body": parts[i+1]})
    return nodes

def subtree(nodes, idx):
    """Return node idx plus all following nodes deeper than it."""
    base = nodes[idx]["lvl"]; out = [nodes[idx]]
    for n in nodes[idx+1:]:
        if n["lvl"] <= base: break
        out.append(n)
    return out

def render(tree, show_title):
    base = tree[0]["lvl"]; h = ""
    for j, n in enumerate(tree):
        if j == 0:
            if show_title: h += f'<h3>{html.escape(fix(n["title"]))}</h3>'
        else:
            rel = n["lvl"] - base + (3 if show_title else 2)
            tag = "h4" if rel >= 4 else "h3"
            h += f"<{tag}>{html.escape(fix(n['title']))}</{tag}>"
        h += clean(n["body"])
    return h

data = []
used_all = {}
for kid, name, short, secs in SYL:
    nodes = parse(f"{SRC}/{name.split()[0] if kid!='hr' else 'HR'}.html")
    used = set()
    titles = [n["title"] for n in nodes]
    kb = {"id": kid, "name": name, "short": short, "secs": []}
    for key, sname, pts in secs:
        # section intro: body of the H1 for this section
        intro = ""; mn = ""
        for n in nodes:
            if n["lvl"] == 1 and n["title"].upper().startswith(key + ":"):
                m = re.search(r"\(([^)]*)\)", n["title"]); mn = m.group(1) if m else ""
                if key not in ("S", "E") or kid != "hr":
                    intro = clean(n["body"])
                if key == "E" and kid == "hr":
                    eb = n["body"]
                used.add(titles.index(n["title"]))
        sec = {"key": key, "name": sname, "mn": "", "intro": intro, "pts": []}
        for k, p in enumerate(pts):
            t, pmn, subs, heads = p[:4]; extra = p[4] if len(p) > 4 else {}
            h = ""
            title0 = t.partition(" – ")[0]
            if kid == "hr" and title0 in HR_STRAT:
                h = clean(HR_STRAT[title0]); extra = {}
            elif heads == ["__E__"]:
                eb2 = re.sub(r"<li>[^<]*slides give no further detail.*?</li>", "", eb, flags=re.S)
                h = clean(eb2)
            else:
                for hd in heads:
                    cands = [i for i, n in enumerate(nodes) if n["title"] == hd and i not in used]
                    if not cands: sys.exit(f"missing heading {kid}: {hd}")
                    tr = subtree(nodes, cands[0])
                    for n in tr: used.add(nodes.index(n))
                    h += render(tr, len(heads) > 1)
            title, _, rest = t.partition(" – ")
            sec["pts"].append({"id": f"{kid}-{key.lower()}{k+1}", "t": title[0].upper() + title[1:], "rest": rest,
                               "subs": [[x, next((m for k2, m in SUBMN.items() if x.startswith(k2)), "")] for x in subs],
                               "mn": PMN.get((kid, title), pmn), "html": h, "related": extra.get("related", [])})
        kb["secs"].append(sec)
    left = [n["title"] for i, n in enumerate(nodes) if i not in used and n["lvl"] > 1]
    if left: print("UNUSED", kid, left)
    data.append(kb)

open(OUT, "w", encoding="utf-8").write("const KBFS = " + json.dumps(data, ensure_ascii=False) + ";\n")
print("ok", sum(len(s["pts"]) for k in data for s in k["secs"]), "points")
