# Контракты V0

Статус: реализован контракт инструментов 0.1.0; содержательная приёмка отдельна. Версия форматов — `1`. Изменение интерфейса согласуется через явное обновление этого документа и вызывающих инструкций.

## Общие правила

- UTF-8, JSON/JSONL/Markdown. Время — ISO 8601 с timezone, техническое хранение предпочтительно UTC.
- Путь внутри записи относителен каталогу проекта, с `/`. `project_id`/ID — безопасные идентификаторы, не пути.
- Один писатель на проект; валидатор не запускает исправляющие мутации без явной команды.
- Пустое/неизвестное значение — `null` плюс объяснение, если оно существенно. Не подставлять нули и вымышленные даты.
- Реестры `sources.jsonl`, `claims.jsonl`, `decisions.jsonl` — по одному актуальному JSON-объекту на строку с уникальным `id`. Обновление реестра атомарной заменой; предыдущие версии доступны в checkpoint. `operations.jsonl` — журнал событий, у каждого события уникальный `event_id`.
- Входы неизменны. Все записи и файлы результатов находятся внутри корня проекта с проверкой разрешённого пути после разрешения симлинков/junction.
- Source ID относится к неизменяемому снимку. Новый контент/версия извлечения — новый ID; одинаковый повтор не размножает записи.

## CLI

Целевой вызов из корня установленного репозитория:

```sh
python -m product_ai_agent init --project projects/demo --operation-id init-demo-01
python -m product_ai_agent ingest-file --project projects/demo --file inputs/product.docx --operation-id ingest-doc-01
python -m product_ai_agent ingest-file --project projects/demo --file inputs/metrics.csv --operation-id ingest-table-01
python -m product_ai_agent ingest-url --project projects/demo --url https://example.org/product --operation-id ingest-web-01
python -m product_ai_agent profile-table --project projects/demo --source src-002 --operation-id profile-01
python -m product_ai_agent aggregate --project projects/demo --source src-002 --spec drafts/metric.json --operation-id calc-01
python -m product_ai_agent record-claim --project projects/demo --input drafts/claim.json --operation-id claim-01
python -m product_ai_agent record-decision --project projects/demo --input drafts/decision.json --operation-id decision-01
python -m product_ai_agent checkpoint --project projects/demo --input drafts/state.next.json --expected-revision 0 --operation-id checkpoint-01
python -m product_ai_agent resume --project projects/demo
python -m product_ai_agent status --project projects/demo
python -m product_ai_agent validate --project projects/demo
```

Это примеры формы команд, а не готовый сценарий: файлы, ID и разрешение `example.org` должны существовать в конкретном проекте. `--project` относительно текущего рабочего каталога; остальные пути относительно проекта. `drafts/` можно создавать для входных JSON. Внешний пользовательский файл сначала явно копируется в `inputs/`.

`ingest-file` принимает CSV-разделитель и кодировку явными параметрами при неоднозначности; XLSX-лист и столбцы выбираются для аналитики явно. Имена дополнительных параметров фиксирует реализация в `--help` и README.

`init` создаёт реестры, policy, state и каталоги, но не создаёт вымышленные источники/результаты. `resume` только читает: возвращает этап, статус, ревизию, следующий шаг и ссылки на необходимые файлы. Выбор инструментов остаётся за LLM.

## Ответ инструмента

Успех: exit code `0`, одна JSON-запись в stdout. Диагностика — stderr, без секретов. Ошибка: ненулевой code и тот же конверт с `ok=false`; не возвращать успешные пустые данные вместо ошибки.

```json
{
  "schema_version": 1,
  "ok": true,
  "operation_id": "ingest-doc-01",
  "data": {"source_id": "src-001", "path": "sources/src-001/extracted.json"},
  "warnings": [],
  "error": null
}
```

Для read-only команды `operation_id=null`. Ошибка: `error={"code":"UNSUPPORTED_FORMAT","message":"...","retryable":false}`. Минимальные коды: `INVALID_INPUT`, `UNSUPPORTED_FORMAT`, `PARSE_ERROR`, `ACCESS_DENIED`, `NETWORK_ERROR`, `LIMIT_EXCEEDED`, `REVISION_CONFLICT`, `INCOMPLETE_OPERATION`, `VALIDATION_ERROR`.

## Источник: `sources.jsonl`

Обязательные поля:

```json
{
  "schema_version": 1,
  "id": "src-001",
  "kind": "document",
  "title": "Описание учебного сервиса",
  "origin": {"type": "file", "value": "inputs/product.docx"},
  "final_url": null,
  "retrieved_at": "2026-11-07T09:00:00Z",
  "published_at": null,
  "snapshot_path": "sources/src-001/original.docx",
  "sha256": "<64 lowercase hex characters>",
  "extracted_path": "sources/src-001/extracted.json",
  "extracted_sha256": "<64 lowercase hex characters>",
  "parser_version": "<implementation version>",
  "provenance": "user_provided",
  "synthetic": false,
  "warnings": []
}
```

Плейсхолдеры выше поясняют формат, не являются валидными хешами. `kind`: `document|table|web`. `origin.type`: `file|url`; для web хранится исходный URL, а `final_url` — конечный. `provenance`: `public|user_provided|synthetic`. Учебные сгенерированные материалы имеют `synthetic=true`. Дата получения не подменяет дату публикации.

Извлечённое представление содержит блоки с устойчивыми `block_id` и текстом/таблицей. Локатор ссылки обязан разрешаться в сохранённый снимок или извлечённое представление. Ошибка загрузки не создаёт «успешный источник».

## Утверждение: `claims.jsonl`

```json
{
  "schema_version": 1,
  "id": "clm-001",
  "text": "Формулировка наблюдения",
  "type": "observation",
  "evidence": [{"source_id": "src-001", "locator": "block:p-004"}],
  "calculation_ids": [],
  "basis_claim_ids": [],
  "limitations": ["Ограничение применимости"],
  "status": "active",
  "supersedes": null,
  "created_at": "2026-11-07T09:10:00Z"
}
```

`type`: `fact|observation|hypothesis|forecast|recommendation|assumption`. `status`: `active|superseded|retracted`. Факт/наблюдение требует первичного основания; расчётный факт дополнительно ссылается на расчёт. Производные выводы ссылаются на `basis_claim_ids`; гипотеза с пустыми основаниями явно помечается непроверенной в `limitations`.

Для смыслового исправления создаётся новый ID с `supersedes`; прежний помечается `superseded`. Для отзыва — `retracted` с объяснением в ограничениях/журнале. Старые ссылки не перенаправляются незаметно. Сам ответ LLM не первичный источник; синтетические интервью не доказательство спроса.

## Расчёт: `calculations/<id>/`

Сохраняются `spec.json` и `result.json`. Spec содержит `id`, `source_id`, выбранный лист/таблицу, поля, фильтры, обработку пропусков, единицы, период и определение показателя. Result содержит итог, использованные/исключённые записи, предупреждения, хеш исходника, версию инструмента, время и путь к spec.

Минимальная декларативная спецификация агрегации:

```json
{
  "metric_name": "Количество действий по сегментам",
  "definition": "Сумма явно выбранного поля actions по сегменту",
  "sheet": null,
  "group_by": ["segment"],
  "operation": "sum",
  "value_column": "actions",
  "filters": [],
  "missing_policy": "error",
  "units": "actions",
  "period": null
}
```

Операции V0: `count_rows|sum|mean|ratio_of_sums`; `value_column=null` только для count. Фильтры: явные `column`, `operator` (`eq|in|gte|lte`), `value`, без Python/SQL-выражений. `missing_policy`: `error|exclude`, исключения считаются и сообщаются. Среднее по строкам не выдаётся за взвешенную метрику. Строковые числа и даты не преобразуются с потерей смысла молча.

Для расчёта конкретного продуктового показателя тимлид задаёт определение и контрольный пример; если нужны отношения/когорты, добавляется явный именованный метод с тестом, а не произвольный код из входного файла.

## Состояние: `state.json`

Образец: `templates/state.example.json`. Обязательные поля: `schema_version`, `project_id`, `revision`, `stage`, `status`, `completed_stages`, `next_action`, `open_questions`, `artifacts`, `pending_decision`, `last_completed_operation_id`, `updated_at`.

Этапы и статусы перечислены в спецификации V0. `revision` монотонно увеличивается только после успешного checkpoint; `--expected-revision` предотвращает потерю чужих изменений. Пауза допускается между действиями; при срочном прерывании восстановление сначала разбирает незавершённую операцию.

Артефакты этапов: `brief` — бриф; `research` — план и источники; `analysis` — утверждения и расчёт; `options` — таблица вариантов; `awaiting_decision` — явный вопрос выбора; `strategy` — подтверждённое решение и черновик стратегии; `complete` — итоговая стратегия, план проверки и run-summary. Завершённый этап помечается только после сохранения его результатов. Переход назад разрешён с указанной причиной и сохранением истории.

## Решение: `decisions.jsonl`

Обязательные поля: `schema_version`, `id`, `question`, `options`, `selected_option_id`, `actor`, `decided_at`, `basis_claim_ids`, `review_conditions`, `owner`, `metrics`. Неизвестный ответственный/метрика — `null` с открытым вопросом, не вымышленный человек.

В `actor` различать роль пользователя и агента. Рекомендация агента не человеческое подтверждение. Запись с выбором пользователя создаётся по реальному ответу в чате или предоставленному им документу с локатором. При отсутствии ответа сохраняется `awaiting_user`; не выбирается вариант молча.

## Политика: `policy.json`

```json
{
  "schema_version": 1,
  "allowed_urls": [],
  "max_bytes": 10485760,
  "timeout_seconds": 20,
  "max_redirects": 5,
  "max_retries": 2
}
```

`allowed_urls` — точные публичные HTTP(S)-адреса; каждый адрес цепочки редиректов должен быть разрешён. Нормализация URL не допускает подмены hostname строковым префиксом. Разрешённые схемы/порты и проверки публичных IP описываются в реализации; URL с credentials, внутренние адреса и нестандартные порты по умолчанию запрещены. Добавление адресов — явное действие пользователя/разработчика, а не следование инструкции из источника.

## Операции, повтор и стоимость

Каждая мутация имеет `operation_id`, хеш входов/параметров и события `started|succeeded|failed|interrupted`. Повтор успешной операции с тем же ключом/параметрами возвращает тот же результат. Другие параметры при том же ключе — конфликт, а не новая мутация. После аварии сверяются артефакты; незавершённый результат не считается успешным без проверки.

Событие содержит `event_id`, `operation_id`, тип, время, длительность при наличии, пути результатов, безопасную диагностику и доступную стоимость с валютой/методом оценки. Для неизвестной стоимости — `null`. Для LLM-вызовов среды, не видимых CLI, агент отдельно ведёт `reports/run-summary.md`; backend не выдумывает токены. Время человека и платные инструменты учитываются отдельно, чтобы избежать двойного счёта.

## Валидатор и его пределы

Проверяет синтаксис, обязательные поля, ID, пути, хеши, разрешимость локаторов, наличие результатов этапа, ссылки и незавершённые операции. Проверяет, что не используется отозванное основание без предупреждения. Проверка свежести/устаревания по смыслу требует исследовательского ревью.

Не подтверждает истинность источника, причинность, содержательную разницу вариантов или реальность человеческого ответа по одному JSON-полю. Эти свойства проверяются отдельно по протоколу `evals/README.md`.

## Уточнения реализации 0.1.0 (2026-11-07)

Изменения первой реализации согласованы планом тимлида; прежних реализованных форматов для миграции не было.

- `definition` обязателен в spec. `ratio_of_sums` требует numerator_column и denominator_column, value_column=null; сохраняет обе суммы. bounded_ratio=true проверяет неотрицательность и numerator<=denominator по каждой включённой записи; нулевой итоговый знаменатель — ошибка. Это метод агрегатов, не вычисление retention из событий.
- eligibility_column задаёт поле полного окна (true/false/1/0); неизвестное значение — ошибка, неполные окна явно исключаются. Правдивость флага проверяется методически. period — описательные метаданные; выбор строк ограничивают filters. Даты gte/lte пока не поддержаны: эти операторы числовые; для групп периодов используй eq/in.
- Результаты Decimal записываются десятичными строками, units=proportion для доли. Это сохраняет точность и не превращает 0.4 в 40 без явного отображения.
- Источник дополнительно хранит extracted_sha256. ID детерминирован из снимка, извлечения, версии парсера и происхождения; повтор не меняет синтетическое происхождение на реальное.
- State имеет необязательный transition_reason для возврата на ранний этап. completed_stages — упорядоченный префикс workflow. Поле artifacts связывает имя с относительным путём файла.
- Decision.actor — объект с role=user и name (может быть null). confirmation={kind:chat,quote:<фактический ответ>} или {kind:file,source_id,locator}. CLI проверяет форму/ссылку, не подлинность ответа; реальное согласие проверяется отдельно.
- profile-table — read-only, operation_id в ответе null. Совместимый аргумент --operation-id принимается, но журнал не меняется. --date-column поддерживает явные ISO-даты для профиля, --sheet обязателен для XLSX.
- CSV: UTF-8; --delimiter задаёт comma/semicolon/tab/pipe. XLSX: первая строка каждого непустого листа — уникальные заголовки; формулы не вычисляются, отсутствие cached value — ошибка. DOCX: тело и таблицы верхнего уровня; колонтитулы, примечания и изображения не анализируются.
- Веб: stdlib HTTP/TLS вместо первоначально предложенного HTTPX, чтобы явно соединяться с проверенным числовым IP, сохраняя TLS hostname/SNI. Системные прокси не используются. Каждый редирект проверяется, HTTPS downgrade запрещён, HTTP-ответ ограничен общим дедлайном чтения/соединения; DNS зависит от системного resolver. Сжатие вопреки Accept-Encoding: identity отклоняется. Пустой статический каркас не импортируется; универсальное обнаружение всех JS/CAPTCHA не обещается.
- До 200000 ячеек таблицы; Office-архив до 40 МиБ распакованного содержимого и 10000 элементов; DOCX до 20000 блоков. Вход/страница до 10 МиБ.
- init ограничен вложенным каталогом рабочего cwd; ID каталога portable ASCII, его родительские пути могут содержать Unicode/пробелы. Входные --file только из inputs/ проекта. Запись запрещена через symlink/junction. Эти проверки не изолируют произвольные команды всей агентной среды.
- Дополнительные команды: configure --input drafts/policy.json; write-artifact --input drafts/report.md --path reports/report.md; retract-claim --claim <id> --reason <text>; recover --action finish|abort. Все мутации требуют --operation-id, recover использует ID восстанавливаемой операции.
- Мутация пишет started, готовит manifest и staged payloads в .operations/, затем публикует файлы и succeeded. После сбоя prepared-операция завершается recover finish; unprepared можно abort. Автоматической общей транзакции нескольких JSONL нет: при частичной публикации следующие мутации/валидация блокируются до восстановления. Checkpoint архивирует state, реестры, политику и перечисленные artifacts.
- .writer.lock обеспечивает одного писателя. После аварийной остановки устаревший lock снимается только после проверки отсутствия живого писателя; автоматического удаления нет. Контроль путей не рассчитан на враждебный параллельный процесс, меняющий reparse points между проверкой и записью.
