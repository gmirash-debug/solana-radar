// Explanations describe evidence, never a trading recommendation or owner identity.
export const TERMS = Object.freeze({
  control_risk: {title:"Control risk", text:"Отдельная оценка концентрации и признаков координации. Даже подтверждённый перевод от общего источника не доказывает одного владельца.", note:"CEX, Relay, роутер, терминал или одинаковая комиссия сами по себе не связывают владельцев. Высокая концентрация — риск, а не причина покупать."},
  rotation: {title:"Market-mediated rotation", text:"Одна группа адресов продаёт, а другие с общим подтверждённым источником финансирования откупают близкий объём примерно по той же цене вскоре после продаж.", note:"Это гипотеза переезда позиции через рынок, не прямой перевод. Повторный оборот не считается новым накоплением; общий владелец не установлен."},
  balance_cap: {title:"Original-position balance cap", text:"Верхняя оценка того, сколько первоначально купленной позиции могло остаться в отслеживаемых кошельках. Новая покупка после снижения не восстанавливает старую позицию в расчёте.", note:"Не является полной историей движения токенов. Снижение может быть продажей или переводом; получатели перевода могут продолжать держать токены."},
  position: {title:"Position left", text:"Какая доля отслеживаемых покупок группы остаётся на последней проверке. Например, 75% означает три четверти той позиции, а не 75% всего суплая.", note:"Original cohort — фиксированная исходная группа; Observed buyers — пока только замеченная выборка. Снижение баланса может быть переводом, а не продажей; диапазон означает неточную долю."},
  retained_supply: {title:"Retained supply", text:"Доля общего выпуска, отнесённая к оставшейся позиции отслеживаемой группы. Смотри тип оценки: Up to — верхняя граница, At least — подтверждённый минимум, диапазон — неопределённость.", note:"Это другой знаменатель, чем у Position left. Балансы без полной истории не доказывают точное удержание первоначальных покупок. Unknown не означает ноль."},
  cohort: {title:"Original cohort", text:"Зафиксированная группа покупателей исходного сигнала. По ней отслеживается, сохранилась ли первоначальная аккумуляция.", note:"Новые покупатели не заменяют тех, кто вышел. При неполном покрытии вывод относится только к сохранённой части группы."},
  holding: {title:"Holding", text:"Исходные покупатели сохраняют позицию по доступным проверкам. Статус описывает удержание, а не появление нового сигнала на вход.", note:"Проверь время балансов, долю удержания и ограничения. Holding не означает, что покупать сейчас выгодно."},
  reduced: {title:"Reduced position", text:"На проверенных адресах исходной группы осталось меньше токенов, чем при сигнале.", note:"Без подтверждённого обмена нельзя утверждать, что токены проданы: они могли перейти на другие адреса."},
  candidate: {title:"Candidate / Watch", text:"Активность прошла предварительный отбор, но всех подтверждений для готового сигнала ещё нет.", note:"Это очередь исследования, не рекомендация покупать. Причина ожидания указана в ограничениях карточки."},
  ready: {title:"Ready to review", text:"Для текущего сигнала доступны подтверждение, свежие балансы и рыночные данные согласно правилам сканера.", note:"Готово к ручному разбору, а не к автоматической покупке. Прибыльный вход не гарантирован."},
  needs_data: {title:"Check needed", text:"Нужная проверка отсутствует, устарела, не завершилась или покрыла недостаточно данных.", note:"Не означает ни продажу, ни отсутствие накопления. Пока проверка не завершена, вывод остаётся ограниченным."},
  hot: {title:"Hot reactivation / Fast move", text:"Сильная ранняя активность с быстрым движением либо неполным подтверждением ATH. Это сигнал повышенного внимания, а не спокойный вход.", note:"Сильная активность и хорошая цена входа — разные вещи. Сам по себе статус не доказывает удержание или связь покупателей."},
  strength: {title:"Strong signal", text:"Активность прошла более сильные пороги отбора в момент этого события.", note:"Историческая сила сигнала не означает, что он подтверждён и актуален сейчас. Для текущего вывода нужны свежие проверки."},
  closed: {title:"Closed / Inactive", text:"Исходный тезис накопления признан утраченным по завершённым проверкам группы.", note:"Не означает, что токен умер или все держатели вышли. Вывод относится к отслеживаемой исходной позиции."},
  noise: {title:"Low confidence", text:"Доступные признаки слабые или только поддерживающие; сильное накопление не установлено.", note:"Низкая уверенность не доказывает отсутствие покупок. Высокая цена токена позднее не делает этот ранний сигнал подтверждённым задним числом."},
  late: {title:"Late / chase", text:"Сигнал появился после заметного движения или при перегретом объёме. Цена входа может быть хуже исходной.", note:"Это предупреждение о погоне за движением, а не доказательство, что рост закончился."},
  confirmation: {title:"Signal confirmation", text:"Прошла ли первоначальная активность проверки подтверждения сканера. Это отдельная проверка от текущих балансов.", note:"Not confirmed не значит, что покупок не было. Даже 100% удержания не подтверждает сигнал автоматически."},
  holders: {title:"Wallets still holding", text:"Сколько сохранённых покупателей исходной группы всё ещё удерживают значимую часть отслеживаемых покупок по правилам сканера.", note:"Это число кошельков, а не доля объёма. Крупный и маленький покупатель здесь считаются как один адрес каждый."},
  retention_checks: {title:"Retention checks", text:"Число проведённых проверок удержания позиции исходной группы.", note:"Смотри время и покрытие: несколько неполных или старых проверок не равны свежему подтверждению всего объёма."},
  checked: {title:"Checked", text:"Время фактической проверки этих данных: балансов, рынка или связей. Оно может отличаться от времени последнего скана.", note:"Обновление страницы не обновляет ончейн-балансы. Overdue / previous означает, что показана прошлая проверка."},
  coverage: {title:"Coverage / Verification", text:"Какая часть нужных кошельков и купленного объёма реально проверена. Покрытие кошельков и покрытие токенов считаются отдельно.", note:"100% сохранённых балансов не обязательно означает 100% исходных покупателей. Важно также покрытие Original cohort."},
  partial: {title:"Partial / Unverified", text:"История, атрибуция покупок или проверка покрывает только часть данных; точный общий вывод пока недоступен.", note:"Unknown / pending — не ноль. Complete относится к указанной проверке, а не ко всей истории токена."},
  mcap: {title:"Market cap (MCAP)", text:"Оценка капитализации: цена токена, умноженная на используемое источником предложение. Caught — при обнаружении, Current — на рыночной проверке.", note:"Методика предложения зависит от источника. Это не ликвидность и не сумма денег, вложенных покупателями."},
  fdv: {title:"FDV", text:"Полностью разводнённая оценка: цена токена × общее предложение. Может отличаться от капитализации обращения.", note:"Не путай FDV с ликвидностью. Свежесть котировки проверяется отдельно от балансов."},
  since_catch: {title:"Since catch", text:"Изменение рыночной оценки токена относительно зафиксированного обнаружения.", note:"Это не фактическая доходность кошельков: время их покупок, цена исполнения, продажи и комиссии могут отличаться."},
  ath: {title:"ATH market cap", text:"Наибольшая капитализация в доступной истории выбранного источника. Дата и источник важны для проверки максимума.", note:"Наблюдавшийся максимум не всегда полный ATH. Нельзя выдавать максимум цены за максимум капитализации."},
  ath_price: {title:"ATH price", text:"Максимальная цена одного токена по данным источника. Это цена, а не капитализация.", note:"Дата пика и полнота истории могут быть не подтверждены. Нельзя просто переносить этот максимум в MCAP."},
  phase: {title:"Market phase", text:"Положение текущей оценки относительно доступного максимума: нижняя, средняя, верхняя зона или около ATH.", note:"Зона описывает относительную цену. Она не доказывает накопление, разворот или безопасный вход."},
  wave: {title:"Buy wave", text:"Волна покупок в выбранном окне: покупатели, объём покупок и продаж, чистый приток.", note:"Большой оборот может включать повторные покупки. Он не равен текущему удержанию и не доказывает общий контроль."},
  gross: {title:"Gross bought supply", text:"Суммарно купленный объём в исследуемом окне, выраженный в процентах общего выпуска.", note:"Повторные покупки учитываются повторно. Это не текущий баланс: для удержания смотри Retained supply."},
  flow: {title:"Observed flow", text:"Объём замеченных сканером покупок в исследуемых окнах. Это активность, а не оставшаяся позиция.", note:"SOL / USD здесь не означают текущую стоимость кошельков или их прибыль."},
  raw_top: {title:"Raw top 10", text:"Доля общего выпуска на крупнейших токен-аккаунтах до исключения резервов пула и других служебных адресов.", note:"Токен-аккаунт не всегда отдельный человек. Резерв пула может создавать высокую сырую концентрацию."},
  circulating: {title:"Estimated circulating supply", text:"Оценка концентрации после исключения установленных резервов пула, сожжённых и других исключённых токенов.", note:"Это приближение к доступному обращению, не гарантированно точный free float. При неразрешённых резервах оценка ограничена."},
  top_cohort: {title:"Signal cohort in top holders", text:"Доля общего выпуска у адресов исходной группы, найденных среди проверенных крупных держателей.", note:"Это только выборка топ-холдеров, не вся удерживаемая позиция группы. 0% здесь не означает, что группа всё продала."},
  cluster: {title:"Linked cluster", text:"Группа адресов с обнаруженными признаками связи; показана доля суплая на проверке.", note:"Связь не равна доказанному общему владельцу. Нулевой кластер или отсутствие флага относится только к проверенной выборке."},
  concentration: {title:"Supply watch / concentrated", text:"В проверенной выборке есть концентрация суплая или признаки связанной группы, требующие внимания.", note:"Смотри конкретный объём, источник связи и покрытие. Высокая концентрация адресов не доказывает, что ими управляет один человек."},
  attributed: {title:"Signal-attributed", text:"Токены или покупки, которые удалось отнести к конкретному сигналу и его покупателям.", note:"Не включает автоматически весь баланс адреса, старые позиции и покупки вне проверенного окна."},
  coordination: {title:"Coordinated activity", text:"Совпадающие признаки покупок нескольких адресов: финансирование, время, исполнитель или другие независимые признаки.", note:"Это наблюдаемый паттерн, не доказательство инсайда или точного бандла. Общий роутер, CEX, Relay или LI.FI сам по себе не связывает владельцев."},
  funder: {title:"Common funder", text:"У нескольких адресов найден общий источник пополнения в проверенной истории.", note:"Источник может быть биржей или сервисом. Без дополнительной связи общий плательщик не доказывает одного владельца."},
  executor: {title:"Common executor", text:"В покупках нескольких адресов повторяется исполнитель или подписант транзакций.", note:"Общий сервис исполнения может обслуживать разных людей. Нужны независимые подтверждения связи."},
  priority: {title:"Priority-fee fingerprint", text:"У транзакций совпадает характерный размер приоритетной комиссии.", note:"Одинаковые настройки бота или сервиса могут совпадать у несвязанных покупателей. Это только поддерживающий признак."},
  fresh: {title:"Fresh", text:"В доступной истории до покупки у адреса не обнаружены предыдущие транзакции.", note:"Свежий адрес не означает нового человека или инсайдера. Вывод зависит от полноты истории."},
  freshish: {title:"Freshish", text:"У адреса мало предыдущих транзакций по правилам сканера.", note:"Слабая история адреса — признак для проверки, а не доказательство намерений или связи с другими покупателями."},
  dormant: {title:"Dormant", text:"До покупки у адреса была длительная пауза активности относительно настроенного порога.", note:"Пауза оценивается в доступной истории. Само пробуждение адреса не доказывает скрытое накопление."},
  low_tx: {title:"Low tx", text:"У адреса сравнительно мало предыдущих транзакций, но он не относится к более сильным классам fresh / freshish / dormant.", note:"В сканере это поддерживающий, а не самостоятельный сильный признак."},
  sticky: {title:"Sticky buyer", text:"Покупатель удерживает заметную часть замеченных покупок по доступным балансам в исследуемой волне.", note:"Удержание не доказывает инсайд или общий контроль. Нужны свежесть, покрытие и подтверждение исходной группы."},
  wave_buyer: {title:"Wave buyer", text:"Адрес замечен среди покупателей исследуемой волны.", note:"Само участие в волне не делает адрес подозрительным и не означает, что позиция удерживается сейчас."},
  wallet_class: {title:"Wallet class", text:"Классификация истории адреса до замеченной покупки: fresh, freshish, dormant, low_tx или обычный кошелёк.", note:"Это поведенческий признак, не личность и не уверенность в том, что адрес — инсайдер."},
  held: {title:"Held", text:"Доля купленной и отслеживаемой позиции этого кошелька, которая остаётся на последней проверке.", note:"Не процент общего суплая. Баланс мог измениться переводами; полный баланс адреса может включать другие покупки."},
  entry: {title:"Entry", text:"Объём замеченных покупок кошелька, использованный для оценки его позиции.", note:"Это не обязательно полный жизненный объём инвестиций адреса и не его текущий баланс SOL."},
  pnl: {title:"Open return / Open PnL", text:"Оценка доходности оставшейся позиции по доступной себестоимости и последней цене. Return — процент, PnL — сумма.", note:"Не реализованная прибыль. Комиссии, переводы, неполная история и цена исполнения могут менять фактический результат."},
  wallet_edge: {title:"Historical wallet edge", text:"Результаты прошлых сигналов, в которых этот кошелёк был замечен до оцениваемого движения.", note:"Малая выборка, неполная история и отбор успешных токенов могут завышать впечатление. Это не гарантирует будущий успех."},
  cross_chain: {title:"Cross-chain inflow", text:"Покупки из другой сети, для которых установлены маршрут и адрес получения через доступные проверки.", note:"Gross purchases не равны удержанию. Один сервис маршрутизации не означает одного покупателя или владельца."},
  preparation: {title:"Buyer preparation", text:"Совпадения в пополнении и времени подготовки адресов перед покупками.", note:"Проверка ограничена доступными сетями и окнами. Совпадение не доказывает координацию или общего владельца."},
  transfer: {title:"Position transfer", text:"Прослеженные переводы токенов исходной позиции на другие адреса после контрольной точки.", note:"Перевод не продажа и не доказательство, что адрес-получатель принадлежит тому же человеку."},
  sold: {title:"Confirmed sold (min.)", text:"Минимальный объём исходной позиции, для которого в покрытой истории установлена продажа через обмен.", note:"Это нижняя граница. Неотслеженные переводы и более ранние продажи могут остаться неразрешёнными."},
  bounds: {title:"Supply bounds", text:"Нижняя и верхняя оценки удерживаемого объёма при неполной атрибуции переводов и покупок.", note:"Верхняя граница — не подтверждённое удержание. Не следует автоматически использовать её как точное значение."},
  tax: {title:"Buy / Sell tax", text:"Комиссия токен-контракта при покупке или продаже по доступной проверке.", note:"Unknown не означает 0%. Проверка налога не гарантирует, что продажа возможна и условия не изменятся."},
  contract: {title:"Contract risk", text:"Выявленные риски токен-контракта и ограничения проведённых проверок.", note:"No flags означает отсутствие выявленного флага, а не отсутствие любого риска или гарантию продаваемости."},
  gmgn_labels: {title:"GMGN wallet labels", text:"Классификация провайдера: Smart, KOL, Bundler и другие метки адресов.", note:"Это внешние метки, не доказательство инсайда, личности или точного участия в бандле."},
  backlog: {title:"Backlog / History window", text:"Часть истории, которую индексатор ещё не обработал. Окно может начинаться с первого наблюдения, а не с запуска токена.", note:"При отставании ранние покупки могут отсутствовать. Complete относится только к указанному окну."},
  quality: {title:"Signal quality / score", text:"Сводная оценка признаков активности по правилам сканера, с отдельными причинами и штрафами.", note:"Score не является вероятностью роста или процентом уверенности в инсайде. Смотри причины, ограничения и актуальность проверки."},
});

const aliases = {
  control_risk:["Control risk", "Holder scope"], rotation:["Possible market-mediated position rotation", "Sell/rebuy rotation pattern"],
  balance_cap:["Original-position balance cap"],
  position:["Position left", "Original position", "Original buyer position", "Observed buyer position"],
  retained_supply:["Retained supply", "Cohort supply", "Observed supply", "Holding now"],
  cohort:["Original cohort", "Original buyers"], holding:["Holding", "Holding confirmed", "Thesis intact", "Held at last check"],
  reduced:["Cohort reduced", "Weakening", "Reduced positions"], candidate:["Watch", "Candidate", "Activity only", "New buy wave"],
  ready:["Ready to review"], needs_data:["Check needed", "Data check needed", "Recheck due", "Waiting for check", "Check unavailable"],
  strength:["Strong signal", "Actionable"], closed:["Closed", "Inactive"], noise:["Low confidence", "Noise"],
  hot:["Hot reactivation", "Hot", "Fast move"], late:["Late/chase", "Late entry"], confirmation:["Signal confirmation", "Fresh signal"],
  holders:["Wallets still holding"], retention_checks:["Retention checks"],
  coverage:["Original wallets covered", "Stored balances checked", "Verification", "Coverage", "Attribution coverage", "Data quality"],
  mcap:["Caught mcap", "Current mcap", "Provider market cap"], fdv:["Current FDV", "Reported FDV"], since_catch:["Since catch"],
  ath_price:["GMGN ATH price", "ATH price / GMGN", "Below price ATH"], phase:["Phase", "Market phase", "Low range", "Mid-range", "Upper range", "Near ATH", "ATH zone"], wave:["Buy wave", "Relay buy wave", "Cross-chain buy wave", "Trigger window"],
  gross:["Gross purchases", "Gross bought supply"], flow:["Observed flow"], raw_top:["Raw top 10"],
  circulating:["Est. circulating top 5", "Est. circulating"], top_cohort:["Signal cohort"], cluster:["Linked cluster", "No concentration flag in checked set"],
  concentration:["Supply watch", "Supply concentrated"], quality:["Quality", "Signal quality"],
  attributed:["Signal-attributed", "Buy attribution"], coordination:["Coordination", "Coordinated activity", "Coordinated pattern", "Funding-linked pattern", "Supporting coincidence", "Converging link signals", "Common control not established", "No links found in checked subset"],
  funder:["Common funder"], executor:["Common executor"], priority:["Priority-fee fingerprint"],
  wallet_class:["Class", "Wallet setup"], held:["Held"], entry:["Entry"], pnl:["Open return", "Open PnL", "Wallet PnL"], wallet_edge:["Historical wallet edge"],
  sticky:["Sticky buyer"], wave_buyer:["Wave buyer", "Market wave"],
  cross_chain:["Cross-chain inflow", "Verified routes", "Verified purchases"], preparation:["Buyer preparation"],
  transfer:["Where the position went", "At original buyers (min.)", "At transfer recipients (min.)"], sold:["Confirmed sold (min.)"],
  bounds:["Supply max.", "Supply min."], tax:["Buy tax", "Sell tax"], contract:["Contract risk"], gmgn_labels:["GMGN wallet labels"],
  backlog:["History window", "Backlog"], partial:["Partial", "Unverified", "Pending", "Unresolved", "Supply pending", "Supply unverified"],
};
const normal = value => String(value ?? "").trim().replace(/\s+/g, " ").toLowerCase();
const byLabel = new Map(Object.entries(aliases).flatMap(([id, labels]) => labels.map(label => [normal(label), id])));

export function termForLabel(label) {
  const text = normal(label);
  const exact = byLabel.get(text);
  if (exact) return exact;
  if (/^(fresh|freshish|dormant|low_tx)(?:\s+\d+)?$/.test(text)) return text.split(" ")[0];
  if (/^sticky buyer(?:\s+\d+)?$/.test(text)) return "sticky";
  if (/^(wave buyer|market wave)(?:\s+\d+)?$/.test(text)) return "wave_buyer";
  if (/^(common funder|common executor)\s/.test(text)) return text.startsWith("common funder") ? "funder" : "executor";
  if (/\bath\b/.test(text) && !/price/.test(text)) return "ath";
  if (/^(checked|previous check|not checked)\b/.test(text)) return "checked";
  return null;
}

export function tooltipPosition(anchor, size, viewport, margin = 12) {
  const width = Math.min(size.width, Math.max(0, viewport.width - margin * 2));
  const left = Math.max(margin, Math.min(anchor.left, viewport.width - width - margin));
  const below = anchor.bottom + 8;
  const above = anchor.top - size.height - 8;
  const top = below + size.height <= viewport.height - margin ? below
    : above >= margin ? above : Math.max(margin, Math.min(below, viewport.height - size.height - margin));
  return {left, top, width};
}

// Only exact labels in known card surfaces are enhanced; token names and row actions stay intact.
const LABEL_SELECTORS = [
  ".token-detail .detail-metric > span", ".token-detail .evidence-facts > div > span",
  ".token-detail .kv > span:first-child", ".token-detail .section-heading > h3",
  ".token-detail .position-status", ".token-detail .chip", ".token-detail th",
  ".token-detail .evidence-time", ".token-detail .retention-summary small",
  ".token-detail .compact-table td:nth-child(2)", ".review-table-head > span",
].join(",");

export function installTerminology(doc) {
  const win = doc.defaultView;
  const popup = doc.createElement("div");
  popup.id = "radar-term-explanation";
  popup.className = "term-popover";
  popup.setAttribute("role", "tooltip");
  popup.setAttribute("lang", "ru");
  popup.hidden = true;
  doc.body.append(popup);
  let active = null, pinned = false, timer = null;
  const listeners = [];
  const listen = (target, type, fn, options) => {
    target.addEventListener(type, fn, options);
    listeners.push(() => target.removeEventListener(type, fn, options));
  };
  const cancel = () => { win.clearTimeout(timer); timer = null; };
  const hide = () => {
    cancel();
    if (active) {
      const ids = (active.getAttribute("aria-describedby") || "").split(/\s+/).filter(id => id && id !== popup.id);
      if (ids.length) active.setAttribute("aria-describedby", ids.join(" "));
      else active.removeAttribute("aria-describedby");
    }
    active = null; pinned = false; popup.hidden = true;
  };
  const trigger = node => node?.closest?.("button.term-trigger") || null;
  const show = button => {
    const term = TERMS[button?.dataset.term];
    if (!term || !button.isConnected) return;
    if (active !== button) hide();
    cancel(); active = button;
    popup.replaceChildren();
    for (const [tag, className, content] of [["strong", "term-popover-title", term.title], ["p", "", term.text], ["p", "term-popover-note", term.note]]) {
      const el = doc.createElement(tag); el.className = className; el.textContent = content; popup.append(el);
    }
    popup.hidden = false;
    const viewport = {width:win.innerWidth, height:win.innerHeight};
    popup.style.maxHeight = `${Math.max(0, viewport.height - 24)}px`;
    popup.style.width = `${Math.min(340, Math.max(0, viewport.width - 24))}px`;
    const position = tooltipPosition(button.getBoundingClientRect(), popup.getBoundingClientRect(), viewport);
    popup.style.left = `${position.left}px`; popup.style.top = `${position.top}px`;
    button.setAttribute("aria-describedby", [...new Set((button.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean).concat(popup.id))].join(" "));
  };
  const deferHide = () => { cancel(); if (!pinned) timer = win.setTimeout(hide, 180); };
  listen(doc, "pointerover", event => {
    if (event.pointerType === "touch") return;
    if (popup.contains(event.target)) { cancel(); return; }
    const button = trigger(event.target);
    if (button && (!pinned || active === button)) show(button);
  });
  listen(doc, "pointerout", event => {
    if (active && (active.contains(event.target) || popup.contains(event.target))
      && !active.contains(event.relatedTarget) && !popup.contains(event.relatedTarget)) deferHide();
  });
  listen(doc, "focusin", event => { const button = trigger(event.target); if (button) show(button); });
  listen(doc, "focusout", event => { if (trigger(event.target) && !popup.contains(event.relatedTarget)) hide(); });
  listen(doc, "click", event => {
    const button = trigger(event.target);
    if (button) {
      if (active === button && pinned) hide();
      else { show(button); pinned = true; }
    } else if (!popup.contains(event.target)) hide();
  });
  listen(doc, "keydown", event => { if (event.key === "Escape" && active) { hide(); event.preventDefault(); } });
  listen(doc, "scroll", event => { if (!popup.contains(event.target)) hide(); }, true);
  listen(win, "resize", hide);
  return {
    refresh(root) {
      hide();
      for (const label of root.querySelectorAll(LABEL_SELECTORS)) {
        if (label.querySelector(".term-trigger") || label.closest("button, a, summary") || label.querySelector("button, a")) continue;
        const id = label.matches(".retention-summary small") && /total token supply|total supply share unverified/i.test(label.textContent)
          ? "retained_supply" : termForLabel(label.textContent);
        if (!id) continue;
        const button = doc.createElement("button");
        button.type = "button"; button.className = "term-trigger"; button.dataset.term = id;
        button.textContent = label.textContent;
        button.setAttribute("aria-label", `${label.textContent.trim()}: пояснение`);
        label.replaceChildren(button);
        label.removeAttribute("title");
      }
    },
    dismiss:hide,
    destroy() { hide(); listeners.forEach(remove => remove()); popup.remove(); },
  };
}
