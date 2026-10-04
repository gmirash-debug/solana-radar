# Solana Radar: инфраструктура в пределах бесплатных квот

Дата проверки: 4 октября 2026. Статус: основная схема внедрена; legacy backlog и длительная нагрузочная приёмка остаются незавершёнными. Цифры baseline ниже сняты до переключения.

## Статус Внедрения

- Новый Worker переключён на Turso для runtime/checkpoints и очереди. Старые DO/D1 bindings сохранены; новые runtime-записи туда не зеркалируются.
- Локальная интеграционная проверка: 853 Python-теста и 450 JS-тестов прошли. `/health` подтверждает оба SQL backend, dashboard API отвечает HTTP 200. Это не заменяет реальный скан и длительную приёмку.
- Применены SQL-миграции 0002-0005; публикация manifests атомарна, сборка superseded parts имеет grace 48 часов.
- Исправлена indexed keyset-очистка без последовательного чтения больших JSON. Learning/retention вынесены из пяти-минутного discovery в отдельный часовой cron.
- R2 использует предварительно оплаченные пакетные grants; warning80, pre90 и rolling33 сохранены. High-water reservations могут остановить архив раньше фактического биллинга; они не возвращаются после неопределённого исхода.
- Native RPC лимитеры и малые durable grants сохраняют расход до сетевого запроса; Solana/Robinhood делят Alchemy ledger. Known accounts проверяются пакетами с обязательной полной enumeration при снижении, закрытии, mismatch и expiry.
- API-only targeted scans, Pages fallback раз в 6 часов, кеш только при изменении. Общий writer lock оставлен до безопасного разделения mutable state.
- Внешний блокер: 04.10 Cloudflare не отдаёт legacy DO очередь с ошибкой дневных записей. Она не удалена. Новые события сохраняются отдельно в Turso; автоматический bounded sweep продолжится при восстановлении доступа, затем staging перейдёт в основной consumer.
- НЕ выполнены: подтверждение полного восстановления старого backlog, семидневная нагрузочная приёмка, удаление старых больших cache-копий. Удаление кеша запрещено до durable acknowledgement каждой уникальной pending записи.
- Нельзя обещать: полное бесплатное покрытие рынка, точный account billing по оценочным RPC-счётчикам, бесконечный архив или подтверждённую готовность неподдерживаемых RPC/Relay-методов.

## Вывод

Не нужна ещё одна база или ещё четыре RPC. Нужны разделение свежих проверок и восстановления истории, запись изменений вместо полных копий и общий учёт расхода аккаунтов. Текущий набор сервисов подходит для личного сканера с ограниченным набором активно проверяемых токенов. Он не обеспечивает бесплатную проверку всего рынка без пропусков и неограниченное хранение всех транзакций.

Цифры нагрузки ниже являются проектным расчётом, а не обещанием фактического расхода. Перед переключением нужны измерения реальных тарифицируемых единиц и проверка восстановления данных.

## 1. Что проверено сейчас

| Проверка | Результат | Значение для архитектуры |
| --- | --- | --- |
| Публичный dashboard API | HTTP 200, `storage_source=turso_sql`; снимок 04.10, 16:52 UTC | Основной SQL уже обслуживается Turso, а не D1 |
| Последний targeted scan | 6 проверенных пулов, 814 кандидатов; 0 ошибок RPC; 3 неполных окна, 2 разрыва истории | Отсутствие ошибок RPC не означает полное покрытие; глубина проверки тоже ограничена |
| RPC в этом отчёте | Helius, Alchemy, Chainstack, PublicNode | dRPC не присутствует в текущем отчёте; нельзя считать его гарантированно работающим резервом |
| Расход RPC за октябрь в отчёте | Helius 57 152; Alchemy 503 310; Chainstack 45 414 | Это оценка сканера, НЕ показания биллинга аккаунтов; единицы разных провайдеров нельзя складывать |
| R2 budget API | Предохранитель недоступен; `r2_budget_unavailable` | Из-за исчерпанных записей DO архив закрыт безопасно. Это не доказательство достижения 90% месячной квоты R2 |
| Аккаунт Turso | Starter/Free; цикл 03.10-01.11; без платёжного метода | Проверены реальные квоты именно этого аккаунта |
| Turso, экран Last 30 days | 3,94 млн чтений; 152,37 тыс. записей; 651,5 MB базы; 532,05 MB sync | Пока есть запас. Это не прогноз после переноса очереди и не обязательно показатели одного календарного месяца |
| Turso, запрос очистки архива | Среднее 18,44 с, 17 выполнений, около 28,33 тыс. читаемых строк за запрос | Нужны план запроса, индексы и выбор ID без больших JSON, а не только увеличение квоты |
| GitHub cache | 10 443 475 335 байт, около 9,73 GiB; несколько копий outbox примерно по 2,07 GB | Почти весь обычный 10 GiB cache занят; вытеснение кеша угрожает восстановлению незаписанных данных |
| GitHub artifacts, выборка до 100 | Около 1,75 GB; Pages и Robinhood backups | Не считать этот объём подтверждённым платным счётом: требуется проверка аккаунтного биллинга и применимых исключений |
| Последние 100 запусков Actions | 9 deep: среднее полное время 15,6 мин; 25 targeted: 7,2 мин; 47 discovery: 1 мин | Один общий writer lock создаёт риск задержек. Полное время workflow не равно времени удержания блокировки |
| Приватный backup repository | `solana-radar-backups`, активный ежедневный workflow; два последних запуска успешны | SQL-бэкап уже есть; не создавать дубликат. 3 Releases занимают около 275 MB; тела R2 этим SQL-бэкапом не копируются |

Проверены код, расписания, публичные API, GitHub и кабинет Turso. Реальный текущий биллинг Helius/Alchemy/Chainstack, весь аккаунт Cloudflare и тариф GMGN-ключа в этой проверке не подтверждены. Наличие рабочего endpoint не доказывает остаток месячной квоты.

## 2. Лимиты сервисов

Квоты относятся к аккаунтам/организациям согласно правилам каждого сервиса. Создание дополнительных ключей, объектов или баз не умножает бесплатную квоту.

| Сервис | Бесплатная квота / важное ограничение | Предлагаемая роль |
| --- | --- | --- |
| [Turso](https://turso.tech/pricing?frequency=monthly) | 5 GB; 500 млн строк прочитано и 10 млн записано в месяц; 3 GB embedded sync | Канонические текущие данные, компактные события, курсоры, очередь и аналитические результаты |
| [Workers](https://developers.cloudflare.com/workers/platform/limits/) | 100 тыс. запросов/день; 10 ms CPU; 128 MB; 50 внешних subrequests за вызов; 5 cron triggers на free | Лёгкий API, авторизация, диспетчеризация. Не распаковка больших архивов и не полный Learning |
| [Durable Objects](https://developers.cloudflare.com/durable-objects/platform/pricing/) | 100 тыс. строк записано/день, 5 млн прочитано/день; 100 тыс. запросов/день; 13 тыс. GB-s/день; 5 GB всего | Только короткие coordination claims и строгий общий предохранитель R2 |
| [D1](https://developers.cloudflare.com/d1/platform/limits/) | 500 MB на free-базу, 5 GB на аккаунт; [5 млн чтений и 100 тыс. записей/день](https://developers.cloudflare.com/d1/platform/pricing/) отдельно от DO | Существующая резервная конфигурация, не дополнительная живая копия каждой записи |
| [R2 Standard](https://developers.cloudflare.com/r2/pricing/) | 10 GB-month; 1 млн Class A и 10 млн Class B в месяц; бесплатный egress. PUT/LIST = A, GET/HEAD = B; DELETE бесплатен для R2 | Сжатые неизменяемые пакеты доказательств, checkpoints и архив. Не primary API для каждой карточки |
| [GitHub Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions) | Стандартные runners публичного репозитория бесплатны; cache 10 GiB/репозиторий. Для private Free: 2000 мин/месяц; artifact allowance 500 MB разделяется с Packages | Все тяжёлые вычисления; private backup остаётся отдельной небольшой задачей. Не хранить единственную копию outbox в cache |
| [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits) | Сайт до 1 GB; мягкий лимит трафика 100 GB/месяц. Custom Actions deployment не подпадает под мягкие 10 сборок/час | Статический интерфейс и явно датированный последний подтверждённый снимок |
| [Helius](https://www.helius.dev/pricing) | 1 млн credits/месяц; [10 RPC requests/sec, отдельная DAS/Enhanced группа 2/sec](https://www.helius.dev/docs/billing/rate-limits) на Free | Owner enumeration, signatures и адресная история; экономить на повторных проверках известных accounts |
| [Alchemy](https://www.alchemy.com/pricing) | 30 млн CU/месяц; актуальная pricing page: 500 throughput CU/sec. В [другой документации](https://www.alchemy.com/docs/reference/pricing-plans) есть 300: до сверки аккаунта проектировать под 300 | Индексированная Solana history, largest holders и доступная архивная EVM-история. Solana и Robinhood делят квоту аккаунта |
| [Chainstack](https://chainstack.com/pricing/) | 3 млн request units/месяц; 25 requests/sec; 1 node. Обычный запрос = 1 RU, archive = 2 RU при доступности archive | Известные transactions и пакетные accounts; доступ к archive НЕ обещать на Developer Free |
| [GMGN](https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-market/SKILL.md) | Market API Free bucket: rate/capacity 5/5; trenches вес 2, trending 3, обычный kline 2. В старых/общих docs встречаются другие цифры | Один discovery ответ использовать многократно; token context/ATH кешировать; учитывать вес, а не только requests/sec |
| [GeckoTerminal](https://apiguide.geckoterminal.com/faq) | Public API: 30 calls/min | Резерв market/discovery/OHLCV. Один общий limiter для обоих сканеров |
| [DEX Screener](https://docs.dexscreener.com/api/reference) | Pairs/search/token endpoints 300 requests/min; profiles/boosts 60/min; token batching до 30 адресов | Пакетное обновление цен/пулов, резерв discovery; ranked lists не дают доказательства обнаружения всех пулов |
| [Relay](https://docs.relay.link/references/api/api-keys) | Self-serve `/requests/v3`: 200/min на ключ. v2 постепенно ограничивается и [закрывается 24.11.2026](https://docs.relay.link/references/api/api_guides/migrating-to-requests-v3) | Проверять межсетевые покупки только для подходящих транзакций; v3 требует ключ. Наличие нужного ключа ещё надо проверить |
| [LI.FI](https://docs.li.fi/api-reference/rate-limits) | Актуальная endpoint page: прочие public endpoints, включая status, 100/min; quote endpoints имеют отдельное двухчасовое окно | Только status найденных переводов. Читать response headers: официальный llms summary содержит отличающиеся лимиты |
| [GoPlus](https://docs.gopluslabs.io/reference/support) | 30 calls/min | Кешируемая проверка token security; неизвестная/неподдерживаемая сеть не означает безопасный токен |
| [Solana Tracker](https://www.solanatracker.io/data-api) | Free 2500 requests/месяц, 3/sec | Только необязательный редкий fallback при наличии ключа. Не обновлять ATH всех токенов каждый час через него |
| [Bright Data Scraper](https://brightdata.com/pricing/web-scraper) | Опубликованы 5000 records/месяц на free scraper tier; SERP/discover имеет отдельный тариф | Social вне критического пути; проверить фактический продукт/план. Не считать один запрос одним record и не переносить квоту Scraper на SERP |
| [dRPC](https://drpc.org/docs/howitworks/ratelimiting), [PublicNode](https://www.publicnode.com/), Robinhood public, Ordo | Бесплатная гарантированная account quota, archive coverage и SLA для нашего набора методов не установлены | Только дополнительный резерв после проверки методов. Не включать неподтверждённую мощность в гарантированный расчёт |

Для DO `put`, `delete` и alarms тоже расходуют записи. Перенос D1 в Turso сам по себе не освобождает квоту четырёх отдельных DO-компонентов. Дневные DO-квоты сбрасываются в 00:00 UTC; это не месячный сброс R2.

## 3. Предлагаемая архитектура

```text
GMGN / DEX Screener / GeckoTerminal
           |
     Discovery каждые 5 минут
           |
     Turso: registry + небольшие задачи
           |
     Live evidence: GitHub compute + RPC router
           |
     Turso: события / балансы / текущие сигналы
           |                             |
   лёгкий Worker API               отдельные jobs
           |                  history / archive / Learning
      GitHub Pages                       |
                                  R2: сжатые пакеты

DO: только claims и R2 budget reservations
Private backup: существующий отдельный workflow
```

### Turso: хранить то, по чему реально ищем

- `token_current`, `cohort_current`, события сигнала, transfer/sale evidence с tx ID, курсоры и версии декодера.
- Компактная task/outbox queue с уникальным ID, next_attempt_at, lease, попытками и безопасным повторным выполнением.
- Доказательства важного свежего сигнала сохранять прежде, чем подтверждать приём. Не ставить свежий сигнал в зависимость от успешной отправки старого raw archive.
- Не переносить 2 GB сырых JSON outbox целиком в SQL. Старый backlog переносить порциями с проверкой hash/count и подтверждением приёма.
- Один registry entry при появлении; далее записывать содержательные изменения. Не обновлять timestamp 800 строк каждые 5 минут ради отметки «проверено».
- Фоновые baseline summaries объединять в 15-минутные интервалы с min/max/volume/count; обнаруженный burst записывать сразу. Не терять короткий всплеск при агрегировании.
- Для медленного archive cleanup: metadata-only выборка ID, `EXPLAIN QUERY PLAN`, соответствующий составной индекс и чтение JSON только для выбранных ID. Тип индекса окончательно выбрать по реальной схеме и плану.
- Без постоянной полной embedded replication на каждый эфемерный runner: использовать HTTP SQL. Разделить метрики SQL traffic и embedded sync.

### R2: пакетный архив, а не журнал каждого движения

- Изменения записывать в сжатые immutable пакеты; одна версия факта, content hash и manifest в SQL. Не собирать отдельный объект на каждую попытку проверки.
- Последние checkpoints хранить с ограниченной цепочкой восстановления; старые полные версии удалять только после проверки новой и наличия восстановимого backup.
- Исторические уникальные доказательства не удалять под видом очистки кеша. Сначала определить политику долгосрочного хранения.
- Не делать GET/HEAD/LIST для каждого render/token. UI работает с SQL/public snapshot; raw evidence запрашивается отдельно при необходимости.
- Предохранитель остаётся строгим: warning 80%, остановка до 90% любой бесплатной квоты, календарный и скользящий 33-дневный учёт. После календарного сброса возобновление не гарантировано.
- Резервировать небольшой пакет A/B операций и максимально возможные bytes заранее, атомарно, через существующий единственный authority. Неиспользованный резерв не освобождать без доказательства. Не выдавать грант, пересекающий учитываемый период, без корректного учёта.
- Уменьшать записи бюджетного счётчика пакетным резервом, а не отключать его. Запись каталога объектов тоже входит в нагрузку DO; учитывать creation, overwrite, delete и восстановление после crash.
- Все writers, включая backup/S3/manual tools, должны проходить тот же guard. Неподконтрольный внешний writer делает локальную гарантию бюджета неполной.

### DO и Worker: оставить маленький control layer

- Убрать живые dashboard mirrors и большие checkpoint copies из `RuntimeSnapshots`, когда Turso/R2 пути проверены.
- Перенести `HistoryQueue` в компактную SQL queue. Не хранить lease/progress/receipt каждого архивного события одновременно в двух системах.
- Оставить `DispatchBuckets` и `R2Budget`; API чтения статуса не должен обновлять счётчик.
- Worker принимает компактные bounded requests; SQL batching с запасом до 50 внешних subrequests. Большие JSON, compression и Learning выполняются на runner, а не внутри 10 ms CPU Worker.
- Чтение dashboard не пишет в БД. Один summary poll раз в 60 секунд при видимой вкладке; карточка грузит свои evidence отдельно. ETag/кеш сокращают SQL, но запрос, дошедший до Worker, всё равно расходует Workers quota.

### GitHub: compute, не основное хранилище

- Discovery и live evidence не должны блокироваться на восстановлении старой истории, Robinhood observer или архивной очистке.
- Отдельные component locks допустимы только после удаления общего mutable state.json из конкурирующих writers. Использовать per-task lease, идемпотентные операции, component revisions и атомарную публикацию manifest.
- Не отменять работающий writer; просроченные одинаковые discovery задачи можно объединять, сохраняя последнюю актуальную проверку. Не терять уникальные history tasks.
- Pages deploy при изменении UI и периодическом обновлении fallback, не после каждого targeted scan. API-данным не нужен повторный deploy HTML.
- Cache содержит только воспроизводимый warm state/dependencies. Удаление любой cache entry не должно терять неподтверждённое событие.
- [GitHub Actions Free](https://docs.github.com/en/actions/reference/limits): 20 одновременно выполняемых jobs; `GITHUB_TOKEN` API quota 1000 requests/hour/repository. Нормальные 384 dispatch/day существенно ниже, но массовый replay не запускать одним неограниченным всплеском. Free Worker уже использует 4 из 5 разрешённых cron definitions; новые контуры можно диспетчеризовать одним cron, не создавая trigger на каждую задачу.
- Сохранить существующий private SQL backup; убедиться, что он восстанавливает данные после нового queue schema. Отдельно проверять доступность ссылочных R2 bodies.
- В public artifacts/cache не помещать ключи, private SQL dumps или пользовательские настройки доступа.

## 4. Расписание и приоритеты

| Контур | План | Что он не делает |
| --- | --- | --- |
| Discovery | Каждые 5 мин; GMGN + пакетный market fallback | Полное RPC-расследование каждой новой пары |
| Live evidence | Каждые 15 мин; свежие material bursts и изменение известных cohorts | Восстановление месяцев истории или обучение |
| Broad discovery audit | Раз в час; текущие примерно 40 глубоких pool slots с отдельным fairness для discovery | Повторная загрузка неизменившейся истории с начала |
| Robinhood | Отдельный live/history job; общий Alchemy accounting с Solana | Удержание Solana writer lock |
| Historical recovery | Асинхронно после свежих задач; отдельные page cursors | Отодвигание live head ради старого хвоста |
| Learning | Раз в сутки по накопленным событиям; дополнительные RPC только для выбранных задач | Повторный on-chain scan всех старых alerts каждый проход |
| Backup / maintenance | Отдельно; metadata queries и проверенное восстановление | Сканирование большого JSON backlog в пользовательском API |

Это целевые интервалы, не гарантированный SLA GitHub/public RPC. Для live измерять возраст фактического evidence и очередь, а не только зелёную отметку workflow. При наплыве приоритет: свежий материальный burst, изменение удержания/исходящих переводов, due thesis, затем старая история и Learning. Ожидание отражается в coverage, а не превращается в «ничего не произошло».

В текущем графике формально до 288 discovery, 72 targeted и 24 deep задач/сутки. Пример с D=15,6, T=7,2, P=1 мин: `24D + 72T + 288P = 1181 минут/сутки`, около 82% одной последовательной линии. Это оценка по полному времени workflow, не измеренная загрузка writer. При максимумах и дополнительных jobs запас исчезает; разделение контуров важнее добавления ещё одного cron.

## 5. RPC routing

| Задача | Основной маршрут | Резерв / правило |
| --- | --- | --- |
| Известные Solana accounts, supply, доступные transactions | Chainstack | Helius, затем Alchemy; только подтверждённые методы и доступная глубина |
| Полный owner enumeration / signatures | Helius | Alchemy; не отправлять методы в Chainstack, который сообщил unsupported |
| Live indexed transaction pages / largest accounts | Alchemy | Helius для поддерживаемой адресной истории |
| Старые launch/transfer gaps | Helius или Alchemy по стоимости и доступности | Отдельная queue; курсор привязан к источнику, failover с restart + dedupe |
| Robinhood logs | Проверенные public endpoints для доступного диапазона | Alchemy для доступной архивной проверки; маленькие log chunks, общий CU ledger |
| Market/ATH/context | GMGN; DEX Screener / GeckoTerminal | Это контекст, не доказательство удержания и не замена RPC |

Важная экономия: известные token accounts проверять через `getMultipleAccounts` пакетами до 100, не через owner enumeration на каждый wallet/mint каждые 15 минут. Периодически заново перечислять owner accounts, а при снижении баланса, закрытии ATA или подозрении на перенос делать это сразу. Проверка одной ATA не доказывает отсутствие токенов на остальных accounts владельца; отсутствие ответа не является нулём.

Иметь два лимитера: мгновенную пропускную способность и месячную стоимость аккаунта. Для Alchemy учитывать throughput CU, не requests/sec; Solana/Robinhood и retries используют общий ledger. Для GMGN учитывать веса методов. Для GeckoTerminal общий плановый темп до 20/min даёт запас до опубликованных 30/min.

[Helius billing](https://www.helius.dev/docs/billing/credits): обычные RPC и `getTransaction` стоят 1 credit; полные `getTransactionsForAddress` стоят минимум 10 и затем 10 за каждые 100 возвращённых транзакций с округлением. `getTransfersByAddress` недоступен на Free. [Alchemy costs](https://www.alchemy.com/docs/reference/compute-unit-costs): owner = 10 CU, largest = 20, signature/transaction = 40, indexed address history = 100; EVM receipt = 20, logs = 60. Batch не делает все входящие методы бесплатными.

## 6. Проверяемый расчёт мощности

Следующие значения - целевой устойчивый профиль после оптимизации, а не новые настройки «останавливать сканер при достижении». Пики поглощаются запасом, маршрутизацией и очередью фоновой работы. Если сам live стабильно дороже этих значений, бесплатный сетап нельзя объявить достаточным.

| Ресурс | Плановая нагрузка | 31 день / запас |
| --- | --- | --- |
| Turso writes, все задачи вместе | До 200 тыс. фактически тарифицируемых строк/день | 6,2 млн / 10 млн; остаётся 3,8 млн |
| Turso reads | До 10 млн строк/день, преимущественно индексированные выборки | 310 млн / 500 млн |
| Turso storage | Плановая рабочая база до 2 GB; raw evidence вне SQL | 3 GB свободно до квоты 5 GB; рост и backlog учитывать отдельно |
| DO writes | Проектная цель менее 10 тыс. строк/день вместе с GC/catalog/alarms | Менее 10% дневной квоты; подтвердить аккаунтными метриками |
| Workers | 10 одновременно открытых вкладок, poll 60 с: 14 400 requests/day + служебные | Значительно ниже 100 тыс./день; 100 постоянно активных вкладок уже превышают квоту |
| Helius | Пример: 10 тыс. owner + 2 тыс. signatures + 800 history pages по 100 tx + 2 тыс. прочего = 22 тыс. credits/day | 682 тыс. / 1 млн; при большем размере страницы стоимость выше |
| Alchemy | Solana примерно 300 тыс. CU/day + Robinhood примерно 250 тыс.; учитывать и все остальные приложения аккаунта | 17,05 млн / 30 млн; запас 12,95 млн до других потребителей и spikes |
| Chainstack | 50 тыс. RU/day; доступную archive операцию считать за 2, не 1 | 1,55 млн / 3 млн |
| R2 operations | Пример пакетного профиля: 500 Class A/day, 5000 Class B/day | За 33 дня: 16 500 A и 165 тыс. B; намного ниже границ guard. Это не замер текущего архива |
| Private backup Actions | Последний запуск 8 мин 40 с; 31 такой запуск около 269 минут | Проверять общий account-wide private allowance; paid plan не требуется для этого профиля при свободном запасе |
| GitHub cache / artifacts | Цель: cache менее 2 GiB; компактные transient artifacts менее 300 MB | Сначала обеспечить durable acknowledgement backlog, только затем убирать старые копии |

### Почему записи в Turso могут уложиться

Нельзя просто перелить все текущие DO-операции в SQL. 814 registry rows каждые 5 минут уже дают 234 432 logical writes/day, до очередей и Learning. Нужны delta updates и 15-минутные агрегаты для неактивных baseline: тот же полный набор каждые 15 минут составляет 78 144 logical writes/day. Live burst events записываются сразу. Фактическую стоимость updates, indices и maintenance подтвердить метриками Turso; внутренний параметр `180000` не равен показанию биллинга.

### Где бесплатность всё равно конечна

R2 storage остаётся конечным, даже если операции почти бесплатны. Пример: 7 GB для уникальной истории + до 1 GB для checkpoints/коротких backups, остальное запас перед 9 GB guard. При условном стартовом архиве 2 GB и приросте 15 MB/day историческая часть 7 GB заполнится примерно за 333 дня; при 50 MB/day - за 100 дней. Текущий размер R2 этой проверкой не установлен, поэтому это сценарии, не прогноз даты остановки.

Для бесконечного архива потребуется локальное долговременное хранилище, согласованная политика удаления восстановимых данных или платное место. Дополнительная бесплатная БД не делает бесконечную историю бесплатной. Standard storage учитывается как средние ежедневные пики за расчётный период; Infrequent Access не использовать как «бесплатный» заменитель.

## 7. Поведение при сбоях

- R2/его guard недоступен: не обращаться к R2; компактные текущие данные и важные evidence сохранять в Turso; архивные задачи остаются незаархивированными. Не обещать полную raw durability эфемерного runner или считать GitHub cache надёжным архивом.
- RPC rate limit: cooldown конкретного provider/method, поддерживаемый резерв, ограниченное повторение с jitter. Не отправлять одинаковые запросы одновременно через все провайдеры.
- Неполное окно/неполный баланс: `partial/unknown` с диапазоном и временем. Не «продали всё», не «нет активности» и не подтверждённый сигнал.
- Turso недоступен: UI показывает последний подтверждённый fallback с его датой; live persistence помечается как неуспешная. Не выполнять незаметный массовый dual-write в D1.
- Learning/maintenance задержался: основное обнаружение продолжает работу. Очередь имеет возраст/объём и возобновляемый курсор; не копит бесконечно один и тот же JSON.
- Независимые GitHub budget/health уведомления сохраняются; heartbeat остаётся дополнительным, не единственным контролем.

## 8. Порядок внедрения и критерии приёмки

1. Снять baseline реальных account quotas/usage; подтвердить GMGN tier, Relay v3 key, RPC capabilities, shared Alchemy spend и текущий R2 inventory без обхода guard.
2. Перенести компактную HistoryQueue и metadata в Turso, убрать дублирующие DO mirrors; исправить тяжёлую SQL выборку. Существующий backlog не удалять.
3. Ввести пакетные R2 reservations и content-addressed archives, проверить восстановление и только потом очистить подтверждённые cache duplicates.
4. Разделить discovery/live/history/Robinhood/Learning writers, сохранив версии и идемпотентность. Ввести общий provider-native accounting и batch account checks.
5. Сделать статус здоровья раздельным: свежесть discovery, полнота live evidence, задолженность history, durable persistence, реальные квоты. Зелёный workflow сам по себе недостаточен.
6. Проверить 7 последовательных суток, включая день после восстановления лимита, нагрузочный replay и отказ R2 на сутки. Очередь live не стареет бесконтрольно, backlog не растёт постоянно, сохраняются события и нет ложных «0»/подтверждений.

Приёмка также включает restart посередине lease, повтор одного event, month boundary и 33-day accounting, concurrency Solana/Robinhood, новое token account после ATA closure, raw/manifest hash validation, выборочную сверку cohort balances и полный restore private SQL backup. Новая схема считается рабочей только после этих проверок, а не сразу после deployment.
