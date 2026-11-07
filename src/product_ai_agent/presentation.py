"""Readable offline decision reports and explicit, local exports."""
import html
import io
import json
import os
import platform
import re
import sys
import zipfile
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .experience import DecisionBrief, LABELS, active_project, check_brief, operation
from .storage import Store, digest, encode, fail, no_links
from .workflow import validation

CSS = """+:root{--ink:#172c39;--muted:#526675;--green:#b9ef65;--line:#dce5e4;--paper:#f3f6f2}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 'Segoe UI',Arial,sans-serif}
button,a{touch-action:manipulation}a{color:#15684d}button:focus-visible,a:focus-visible{outline:3px solid #318b6a;outline-offset:4px}
.wrap{max-width:1180px;margin:auto;padding:0 32px}header{background:#112e38;color:white;padding:28px 0 50px}
.brand{display:flex;align-items:center;gap:12px;font-size:19px;font-weight:650;letter-spacing:-.3px}.mark{background:var(--green);color:#163832;border-radius:11px;padding:5px 12px;font-size:22px}
.topline{display:flex;justify-content:space-between;align-items:center}.version{color:#aec3c5;font-size:13px}
.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:2px;font-weight:700;color:#c0e6cd;margin-top:34px}
h1{font-size:clamp(30px,4vw,47px);line-height:1.13;letter-spacing:-1.5px;max-width:850px;margin:12px 0 18px}
.subtitle{color:#c8d7d8;max-width:800px;margin:0}.tag{display:inline-block;background:#ffffff16;border:1px solid #ffffff32;border-radius:30px;padding:5px 12px;font-size:12px;margin:18px 8px 0 0}
main{padding:28px 0 60px}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:22px}.tab{cursor:pointer;border:1px solid #cad8d3;background:transparent;border-radius:25px;padding:10px 18px;color:var(--ink);font:600 14px 'Segoe UI',Arial}
.tab[aria-selected=true]{background:#163a38;color:white;border-color:#163a38}.grid{display:grid;grid-template-columns:1.65fr 1fr;gap:22px}.card{background:white;border:1px solid var(--line);border-radius:18px;padding:26px;margin-bottom:20px}
.hero-card{border-left:5px solid #56a77e}.label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1.3px;font-weight:700}h2{font-size:23px;line-height:1.25;letter-spacing:-.5px;margin:9px 0 18px}h3{font-size:17px;margin:20px 0 8px}p{margin:0 0 14px}
.recommendation{font-size:23px;line-height:1.45;font-weight:600;margin-top:12px}.reason{padding:16px 0;border-bottom:1px solid #edf1ef}.reason:last-child{border:0}.refs{display:block;margin-top:5px;font-size:12px;color:#467160}
.step{display:grid;grid-template-columns:32px 1fr;gap:12px;margin:20px 0}.step-number{width:28px;height:28px;background:#e8f5dc;border-radius:50%;text-align:center;font-weight:700;color:#2a633e}.step strong{display:block;line-height:1.4}.small{font-size:13px;color:var(--muted)}
ul{padding-left:20px}li{margin:8px 0}.notice{background:#f9f0d8;border:1px solid #ead7a3;border-radius:12px;padding:14px 18px;font-size:14px;margin-bottom:20px}
.panel[hidden]{display:none}.prose h1{font-size:29px;letter-spacing:-.7px}.prose h2{font-size:22px;margin-top:28px}.prose h3{font-size:18px}.prose p{margin-bottom:12px}.bullet{padding-left:16px;border-left:2px solid #d6e7d8}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px;margin:16px 0}th,td{border-bottom:1px solid var(--line);text-align:left;vertical-align:top;padding:11px 10px}th{background:#edf3ed;color:#244f3f}code{background:#edf1ed;border-radius:4px;padding:1px 4px;font-size:12px;overflow-wrap:anywhere}
details{border:1px solid var(--line);background:white;border-radius:12px;padding:16px;margin:12px 0}summary{cursor:pointer;font-weight:600}footer{color:var(--muted);font-size:12px;padding:20px 0;border-top:1px solid var(--line)}.print{background:var(--green);color:#173b31;border:0;border-radius:8px;padding:9px 14px;cursor:pointer;font-weight:650}.metric{font-size:20px;font-weight:700;color:#25634c}.pill{border-radius:20px;background:#e9f2e7;color:#376445;padding:4px 9px;display:inline-block;font-size:12px}
@media(max-width:760px){.wrap{padding:0 18px}.grid{grid-template-columns:1fr}.card{padding:21px}h1{letter-spacing:-.7px}.version{display:none}.recommendation{font-size:20px}}
@media print{body{background:white;font-size:11px}header{padding:10px 0 20px;background:white;color:#173b31}.subtitle,.eyebrow{color:#465b53}.tabs,.print{display:none}.panel[hidden]{display:block!important}.card{break-inside:avoid;border-radius:0;padding:14px}.wrap{max-width:none}.grid{display:block}h1{font-size:29px}.tag{color:#173b31;border-color:#abc}.notice{background:white}.panel{break-before:auto}}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def inline(text):
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = esc(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def markdown(text):
    parts, table, paragraph = [], [], []
    def flush():
        if paragraph:
            parts.append("<p>" + inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()
        if table:
            rows = [line.strip().strip("|").split("|") for line in table]
            rows = [row for row in rows if not all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in row)]
            markup = "<div class='table-scroll'><table>"
            for index, row in enumerate(rows):
                tag = "th" if index == 0 else "td"
                markup += "<tr>" + "".join(f"<{tag}>{inline(cell.strip())}</{tag}>" for cell in row) + "</tr>"
            parts.append(markup + "</table></div>"); table.clear()
    for line in text.splitlines():
        if line.strip().startswith("|"):
            if paragraph: flush()
            table.append(line); continue
        if table: flush()
        heading = re.match(r"^(#{1,6})\s+(.+)", line)
        if heading:
            flush(); level = min(len(heading[1]) + 1, 4)
            parts.append(f"<h{level}>{inline(heading[2])}</h{level}>")
        elif re.match(r"^\s*(?:[-*]|\d+\.)\s+", line):
            flush(); parts.append("<p class='bullet'>" + inline(line.strip()) + "</p>")
        elif not line.strip(): flush()
        else: paragraph.append(line)
    flush()
    return "\n".join(parts)


def decision_markdown(brief):
    b = brief.model_dump()
    lines = ["# " + b["title"], "", "## Что рекомендуем", b["recommendation"], "", "## Почему"]
    lines += ["- " + reason["text"] + " (" + ", ".join(reason["claim_ids"]) + ")" for reason in b["reasons"]]
    lines += ["", "## Надёжность вывода", b["confidence"], "", "## Первые действия"]
    lines += [f"{i}. {s['action']} — {s['when']}. Результат: {s['deliverable']}. Ответственный: {s['owner'] or 'назначить'}." for i,s in enumerate(b["first_steps"], 1)]
    for heading, values in [("Чего пока не делать", b["do_not"]), ("Открытые вопросы", b["unknowns"]), ("Когда пересмотреть", b["revisit_when"])]:
        lines += ["", "## " + heading] + ["- " + v for v in values]
    lines += ["", "## Как поймём результат", b["success_rule"], "", "## Ожидаемый эффект", b["expected_effect"]]
    if b["synthetic"]: lines += ["", "Учебные основания: синтетические сведения не доказывают реальный спрос и эффект."]
    return "\n".join(lines) + "\n"


def report_html(store, brief):
    state = store.state(); b = brief.model_dump()
    sources = store.registry("sources.jsonl"); claims = store.registry("claims.jsonl"); decisions = store.registry("decisions.jsonl")
    steps = "".join(f"<div class='step'><span class='step-number'>{i}</span><div><strong>{esc(s['action'])}</strong><span class='small'>{esc(s['when'])} · {esc(s['owner'] or 'Ответственного нужно назначить')}</span><p>{esc(s['deliverable'])}</p></div></div>" for i,s in enumerate(b["first_steps"],1))
    claim_numbers = {c['id']:i for i,c in enumerate(claims,1)}
    reasons = "".join(f"<div class='reason'>{esc(r['text'])}<span class='refs'>{', '.join('<a href=\"#'+esc(c)+'\">Основание '+str(claim_numbers[c])+'</a>' for c in r['claim_ids'])}</span></div>" for r in b["reasons"])
    def items(values): return "<ul>" + "".join("<li>"+esc(v)+"</li>" for v in values) + "</ul>"
    def doc(name):
        return markdown(store.read(name).decode("utf-8")) if store.path(name).is_file() else "<p>Документ ещё готовится.</p>"
    evidence = "".join(f"<details id='{esc(c['id'])}'><summary>{esc(c['text'])}</summary><p class='small'>{esc(c['type'])} · {esc(c['status'])}</p>" + items([f"{e['source_id']} · {e['locator']}" for e in c['evidence']]) + items(c['limitations']) + "</details>" for c in claims)
    source_rows = "".join(f"<tr><td>{esc(s['title'])}<br><span class='small'>{esc(s['id'])}</span></td><td>{'Учебный' if s['synthetic'] else esc(s['provenance'])}</td><td>{esc(s['origin']['value'])}</td></tr>" for s in sources)
    calc_rows = []
    for file in store.path("calculations").glob("*/result.json"):
        result = store.json(file.relative_to(store.root).as_posix()); spec = store.json(result['spec_path'])
        for group in result['groups']:
            shown = format((Decimal(group['value'])*100).normalize(),'f')+'%' if spec['units']=='proportion' else group['value']+' '+spec['units']
            calc_rows.append(f"<tr><td>{esc(spec['metric_name'])}</td><td>{esc(', '.join(str(v) for v in group['group'].values()) or 'Все записи')}</td><td class='metric'>{esc(shown)}</td><td>{esc(group.get('numerator') or '—')} / {esc(group.get('denominator') or '—')}</td></tr>")
    choice = "<p>Выбор пользователя ещё не получен. Рекомендация агента не заменяет решение.</p>"
    if decisions:
        d = decisions[-1]
        label = next(o.get('label',o['id']) for o in d['options'] if o['id']==d['selected_option_id'])
        choice = f"<p><strong>{esc(label)}</strong></p><p class='small'>{esc(d['decided_at'])}</p><p>{esc(d.get('confirmation',{}).get('quote','Решение сохранено с локатором источника.'))}</p>"
    notice = "<div class='notice'>Учебный кейс. Числа и продуктовые сведения отмечены как синтетические. Проверяем способ работы агента; реальный эффект не заявляется.</div>" if b['synthetic'] else ""
    if store.path('review.json').is_file() and store.json('review.json').get('status')=='pending':
        notice="<div class='notice'><strong>Есть новые данные.</strong> Ниже показано предыдущее решение. Его основания требуют пересмотра; не считайте рекомендацию актуальной до проверки.</div>"+notice
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><meta name='referrer' content='no-referrer'><title>{esc(b['title'])} — Product AI Agent</title><style>{CSS}</style></head>
<body><header><div class='wrap'><div class='topline'><div class='brand'><span class='mark'>↗</span>Product AI Agent</div><span class='version'>Карточка решения · {__version__}</span></div><div class='eyebrow'>От вопроса к следующему действию</div><h1>{esc(b['title'])}</h1><p class='subtitle'>Рекомендация, основания и план — в одном месте. Подробности доступны ниже.</p><span class='tag'>{esc(LABELS[state['stage']])}</span><span class='tag'>{len(sources)} источника · {len(claims)} оснований</span></div></header>
<main><div class='wrap'>{notice}<nav class='tabs' aria-label='Разделы отчёта'><button class='tab' data-panel='overview' aria-selected='true'>Решение за минуту</button><button class='tab' data-panel='plan' aria-selected='false'>Стратегии и проверка</button><button class='tab' data-panel='evidence' aria-selected='false'>Основания</button><button class='tab' data-panel='trace' aria-selected='false'>Ход работы</button></nav>
<section id='overview' class='panel'><div class='grid'><div><article class='card hero-card'><div class='label'>Что делать</div><p class='recommendation'>{esc(b['recommendation'])}</p></article><article class='card'><div class='label'>Почему это разумно</div>{reasons}</article><article class='card'><div class='label'>Первые действия</div>{steps}</article></div><aside><article class='card'><div class='label'>Насколько надёжен вывод</div><p>{esc(b['confidence'])}</p></article><article class='card'><div class='label'>Чего пока не делать</div>{items(b['do_not'])}</article><article class='card'><div class='label'>Как поймём результат</div><p>{esc(b['success_rule'])}</p><p class='small'>{esc(b['expected_effect'])}</p></article><article class='card'><div class='label'>Решение владельца</div>{choice}</article></aside></div><article class='card'><h2>Что ещё нужно решить</h2>{items(b['unknowns'])}<h3>Когда вернуться к рекомендации</h3>{items(b['revisit_when'])}</article></section>
<section id='plan' class='panel' hidden><article class='card prose'>{doc('reports/strategy.md')}</article><article class='card prose'>{doc('reports/validation-plan.md')}</article><details><summary>История сравнения вариантов до выбора</summary><p class='small'>Это сохранённый документ предыдущего этапа. Актуальное решение владельца показано на главной вкладке.</p><div class='prose'>{doc('reports/options.md')}</div></details></section>
<section id='evidence' class='panel' hidden><article class='card'><h2>Воспроизводимые расчёты</h2><p class='small'>Доли показаны в процентах. Исходные суммы, группы и параметры сохранены вместе с расчётом.</p><div class='table-scroll'><table><tr><th>Расчёт</th><th>Группа</th><th>Результат</th><th>Числитель / знаменатель</th></tr>{''.join(calc_rows)}</table></div></article><h2>Утверждения и ограничения</h2>{evidence}<article class='card'><h2>Первичные материалы</h2><div class='table-scroll'><table><tr><th>Источник</th><th>Происхождение</th><th>Откуда получен</th></tr>{source_rows}</table></div></article></section>
<section id='trace' class='panel' hidden><article class='card prose'>{doc('reports/run-summary.md')}</article></section><footer>Product AI Agent · Сформировано по сохранённым файлам · {esc(state['updated_at'])}<br>Условия пересмотра сохранены. Новые данные обрабатываются по запросу пользователя.<p><button class='print' onclick='window.print()'>Печать / сохранить PDF</button></p></footer></div></main>
<script>document.querySelectorAll('[data-panel]').forEach(b=>b.addEventListener('click',()=>show(b.dataset.panel)));function show(id){{document.querySelectorAll('.panel').forEach(p=>p.hidden=p.id!==id);document.querySelectorAll('[data-panel]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.panel===id)));}}document.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',()=>{{const d=document.getElementById(a.hash.slice(1));if(d&&d.tagName==='DETAILS'){{show('evidence');d.open=true;}}}}));</script></body></html>"""


def publish_report(project=None):
    root = active_project(project); store = Store(root)
    report = validation(store)
    if not report['valid']: fail('VALIDATION_ERROR', 'Перед выдачей отчёта нужно исправить целостность исследования.')
    brief = DecisionBrief.model_validate(store.json('reports/decision-brief.json')); check_brief(store, brief)
    params = {'renderer_version':__version__, 'revision':store.state()['revision'], 'brief':brief.model_dump(),
              'documents':{p:digest(store.read(p)) for p in ['reports/strategy.md','reports/options.md','reports/validation-plan.md','reports/run-summary.md'] if store.path(p).is_file()}}
    writes = {'reports/decision.md':decision_markdown(brief).encode(), 'reports/decision.html':report_html(store,brief).encode()}
    params['render_sha256'] = digest(writes['reports/decision.html'])
    store.mutate(operation('report',params),'report',params,lambda:({'report':'reports/decision.html'},writes,[]))
    return {'title':brief.title,'recommendation':brief.recommendation,'report':str(root/'reports/decision.html'),
            'markdown':str(root/'reports/decision.md'),'message':'Готова короткая карточка решения. Подробности — во вкладках отчёта.'}


def doctor(project=None):
    issues=[]
    if sys.version_info < (3,11): issues.append('Требуется Python 3.11 или новее.')
    for name in ['pydantic','docx','openpyxl','bs4']:
        try: __import__(name)
        except ImportError: issues.append('Не установлена зависимость: '+name)
    result={'ready':not issues,'version':__version__,'python':platform.python_version(),'platform':platform.system(),'issues':issues}
    if project:
        report=validation(Store(active_project(project)))
        result.update(ready=result['ready'] and report['valid'],project=report)
    return result


def export_project(project=None, include_sources=False):
    root=active_project(project); store=Store(root)
    check=validation(store)
    if not check['valid']: fail('VALIDATION_ERROR','Нельзя упаковать повреждённое исследование.')
    payloads={}
    if include_sources:
        for directory, dirs, files in os.walk(root,followlinks=False):
            for name in list(dirs): no_links(Path(directory)/name)
            dirs[:]=[name for name in dirs if name not in {'__pycache__'}]
            for name in files:
                path=Path(directory)/name; rel=path.relative_to(root).as_posix()
                if name=='.writer.lock' or name.endswith(('.zip','.tmp')): continue
                payloads[rel]=store.read(rel)
    else:
        for path in store.path('reports').glob('*'):
            if path.suffix in {'.md','.html'}: payloads[path.relative_to(root).as_posix()]=store.read(path.relative_to(root).as_posix())
        if store.path('brief.md').is_file(): payloads['brief.md']=store.read('brief.md')
    manifest={'schema_version':1,'portable':include_sources,'project_id':store.state()['project_id'],
              'version':__version__,'files':{name:digest(data) for name,data in payloads.items()}}
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in payloads.items(): archive.writestr(store.state()['project_id']+'/'+name,data)
        archive.writestr(store.state()['project_id']+'/PACKAGE.json',encode(manifest))
    kind='portable' if include_sources else 'report'
    target=f"reports/{kind}-{digest(encode(manifest))[:12]}.zip"
    params={'manifest':manifest}
    store.mutate(operation('export',params),'export',params,lambda:({'path':target},{target:output.getvalue()},[]))
    return {'path':str(root/target),'portable':include_sources,'files':len(payloads),
            'message':'Полная папка исследования: содержит входные материалы; перед публикацией проверьте права и приватность.' if include_sources else 'Отчёты для чтения. Для продолжения исследования нужна полная папка.'}
