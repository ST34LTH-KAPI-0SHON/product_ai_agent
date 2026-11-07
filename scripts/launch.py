"""One-time setup and a local welcome page. The host app provides the LLM."""
import argparse
import html
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if sys.version_info >= (3, 11):
    import tomllib
    VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
else:
    VERSION = "Python 3.11+ required"
PROMPTS = {
    "demo": "Запусти демонстрацию Product AI Agent. Прочитай instructions/product.md. Создай новое учебное исследование через start --demo; разрешаю https://basecamp.com/ как открытый источник. Сам выполни технические действия, покажи понятный вывод и дождись моего выбора направления. Код не изменяй.",
    "new": "Начать новую задачу с Product AI Agent. Прочитай instructions/product.md. Спроси, какое решение мне нужно принять и какие материалы я могу дать. Используй папку inbox, только если я явно подтвержу её материалы. Код не изменяй.",
    "resume": "Продолжи работу с Product AI Agent. Прочитай instructions/product.md, вызови session, восстанови решение и следующий шаг по сохранённым файлам. Не повторяй завершённые действия и не меняй код.",
}


def welcome_page():
    def link(kind):
        return html.escape("codex://new?" + urlencode({"path": str(ROOT), "prompt": PROMPTS[kind]}), quote=True)
    cards = "".join(f"<a class='choice' href='{link(kind)}'><span class='num'>{i}</span><h2>{title}</h2><p>{description}</p><strong>{action} →</strong></a>" for i,kind,title,description,action in [
        (1,"demo","Посмотреть на примере","Агент разберётся со снижением активации, покажет основания и предложит действия.","Попробовать демо"),
        (2,"new","Разобрать мою задачу","Опишите решение и дайте документы, таблицы или разрешённые ссылки.","Начать исследование"),
        (3,"resume","Продолжить работу","Вернитесь к сохранённому решению, плану и открытым вопросам.","Продолжить")])
    prompt = html.escape(PROMPTS['demo'])
    return f"""<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Product AI Agent — начать</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f2f5ef;color:#17343b;font:17px/1.6 'Segoe UI',Arial}}.wrap{{max-width:1120px;margin:auto;padding:0 32px}}header{{background:#112f38;color:#fff;padding:30px 0 70px}}.brand{{font-size:20px;font-weight:650}}.mark{{display:inline-block;background:#b9ef65;color:#163832;border-radius:10px;padding:1px 12px;margin-right:10px}}.eyebrow{{color:#b6d6c4;font-size:12px;letter-spacing:2px;text-transform:uppercase;margin-top:50px}}h1{{font-size:clamp(36px,5vw,62px);line-height:1.08;letter-spacing:-2px;max-width:780px;margin:16px 0 22px}}.lead{{color:#c8dcdb;max-width:690px;font-size:19px}}.choices{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin-top:-26px}}.choice{{color:inherit;text-decoration:none;background:white;border:1px solid #d6e2d7;border-radius:18px;padding:28px;box-shadow:0 8px 32px #102c3610}}.choice:hover{{transform:translateY(-3px);box-shadow:0 12px 36px #102c3622}}.choice:focus-visible{{outline:3px solid #378960}}.num{{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:50%;background:#e8f6da;color:#2c6d3e;font-size:14px;font-weight:bold}}h2{{font-size:23px;line-height:1.3;letter-spacing:-.5px;margin:18px 0 12px}}.choice p{{color:#556a70;font-size:15px;min-height:78px}}.choice strong{{font-size:15px;color:#286e4c}}section{{margin:36px 0}}.note{{background:#e7efe6;border-radius:14px;padding:22px 26px;font-size:15px}}details{{border:1px solid #d4dfd4;border-radius:14px;padding:20px;background:white}}summary{{cursor:pointer;font-weight:600}}textarea{{width:100%;height:145px;padding:15px;border:1px solid #cad7cf;border-radius:10px;font:14px/1.6 'Segoe UI';resize:vertical}}button{{background:#193d37;color:white;border:0;padding:10px 16px;border-radius:8px;cursor:pointer}}.small{{font-size:13px;color:#61726c}}footer{{font-size:12px;color:#687970;padding:28px 0 40px}}@media(max-width:800px){{.choices{{grid-template-columns:1fr}}.choice p{{min-height:0}}h1{{letter-spacing:-1px}}.wrap{{padding:0 20px}}}}
</style></head><body><header><div class='wrap'><div class='brand'><span class='mark'>↗</span>Product AI Agent</div><div class='eyebrow'>Помощник руководителя продукта</div><h1>Из сложного вопроса —<br>в понятное решение.</h1><p class='lead'>Агент изучит материалы, проверит цифры и сравнит действия. Вы получите рекомендацию, основания и план следующего шага.</p></div></header><main class='wrap'><div class='choices'>{cards}</div><section class='note'><strong>Инструменты готовы.</strong> Кнопка откроет Codex с нужной папкой и подготовленным сообщением. Отправьте его в чате. Во время исследования достаточно отвечать на существенные вопросы — инструменты и сохранение ведёт агент.</section><section><h2>Где будет результат</h2><p>В чате — короткая рекомендация и следующие действия. В папке исследования — читаемая карточка решения, проверяемые основания и план под вашу задачу. Работу можно остановить и продолжить позже.</p></section><details><summary>Использую другую среду или кнопка не открылась</summary><p>Откройте папку репозитория в среде с LLM, доступом к файлам и терминалу. Вставьте сообщение ниже.</p><textarea id='prompt' readonly>{prompt}</textarea><p><button id='copy'>Скопировать сообщение</button></p><p class='small'>Рабочая папка: {html.escape(str(ROOT))}. Для Cursor правила проекта лежат в .cursor/rules. Для Codex — AGENTS.md. Автоматическое подключение в других средах не заявляется.</p></details><footer>Версия {VERSION} · Папка агента, работающая в готовой среде · Для модели нужен доступ/подписка выбранной среды.<br>Наши локальные инструменты отдельного API-ключа модели не требуют. Демонстрационные продуктовые сведения отмечены как учебные.</footer></main><script>document.getElementById('copy').onclick=async()=>{{const t=document.getElementById('prompt');try{{await navigator.clipboard.writeText(t.value);document.getElementById('copy').textContent='Скопировано';}}catch(e){{t.focus();t.select();document.getElementById('copy').textContent='Текст выделен — нажмите Ctrl+C';}}}};</script></body></html>"""


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--no-open',action='store_true')
    parser.add_argument('--skip-install',action='store_true',help='For packaging QA only; normal launch always checks installation')
    args=parser.parse_args()
    if sys.version_info < (3,11):
        raise SystemExit('Требуется Python 3.11 или новее. Проверенная версия: 3.13.')
    os.chdir(ROOT)
    python=ROOT/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    folder=ROOT/'.agent';folder.mkdir(exist_ok=True)
    if not args.skip_install:
        if not python.exists(): subprocess.run([sys.executable,'-m','venv',str(ROOT/'.venv')],check=True)
        probe=subprocess.run([str(python),'-c',f"import product_ai_agent,docx,openpyxl,bs4;from importlib.metadata import version;assert version('product-ai-agent')=='{VERSION}'"],capture_output=True)
        if probe.returncode:
            print('Готовим инструменты. Первый запуск требует интернета к PyPI; следующие запуски используют установленное окружение.',flush=True)
            with (folder/'setup.log').open('w',encoding='utf-8') as log:
                install=subprocess.run([str(python),str(ROOT/'scripts/bootstrap.py')],stdout=log,stderr=subprocess.STDOUT,
                    env={**os.environ,'PYTHONIOENCODING':'utf-8','PIP_DISABLE_PIP_VERSION_CHECK':'1'})
            if install.returncode:
                raise SystemExit('Не удалось подготовить инструменты. Подробности сохранены в .agent/setup.log; передайте файл агенту для разбора.')
        check=subprocess.run([str(python),'-m','product_ai_agent','doctor'],capture_output=True,text=True,encoding='utf-8')
        if check.returncode: raise SystemExit('Проверка инструментов обнаружила проблему: '+check.stdout)
    from_path=folder/'welcome.html';from_path.write_text(welcome_page(),encoding='utf-8',newline='\n')
    (ROOT/'inbox').mkdir(exist_ok=True)
    print('Готово. Открываем страницу начала работы.',flush=True)
    print(str(from_path),flush=True)
    if not args.no_open: webbrowser.open(from_path.as_uri())
    return 0


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
