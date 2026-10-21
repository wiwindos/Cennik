"use strict";
(() => {
  const el = id => document.getElementById("analytics-" + id);
  const names = {vendor_arbitrage:"Vendor Arbitrage", historical_discount:"Historical Discount", simple_craft:"Simple Craft Arbitrage", craft_graph:"CraftGraph"};
  const date = value => value ? new Date(value).toLocaleString("ru-RU") : "неизвестно";
  const money = value => {
    if (value === null || value === undefined) return "неизвестно";
    const copper = BigInt(value);
    return `${copper / 10000n} з ${copper % 10000n / 100n} с ${copper % 100n} м`;
  };
  let settings = null, status = null, offset = 0, revision = 0, listRevision = 0, timer = null;
  const selected = new Map(); let selectedRun = null, selectedMarket = null;
  let localBridge = null, syncPending = null;
  function renderLocalBridge(value) {
    localBridge = value;
    el("local-sync-controls").hidden = !value.available;
    el("local-sync").disabled = !value.ready || Boolean(syncPending);
    el("local-sync-note").textContent = value.ready ?
      `Аддон: ${value.addon_dir}. Для передачи отчётов сначала выполните /reload в игре, затем нажмите синхронизацию; после неё — /reload для загрузки пакетов и подтверждений.` : value.error || "";
  }
  async function synchronize() {
    if (syncPending) return syncPending;
    el("local-sync").disabled = true;
    el("local-sync-note").textContent = "Синхронизация…";
    syncPending = api("/addon/local-sync", {method:"POST"});
    try {
      const result = await syncPending;
      el("local-sync-note").textContent = `Пакетов записано: ${result.packages}. Подтверждено событий за всё время: ${result.acknowledged_events}. Выполните /reload в игре.`;
      return result;
    } catch (exc) {
      el("local-sync-note").textContent = "Синхронизация не завершена: " + exc.message;
      throw exc;
    } finally {
      syncPending = null;
      el("local-sync").disabled = !localBridge?.ready;
    }
  }
  async function deliverPackage(note) {
    if (!localBridge?.ready) {
      el("export-note").textContent = note + " Запустите Bridge, затем /reload в игре.";
      return;
    }
    el("export-note").textContent = note + " Передаём в установленный аддон…";
    try {
      await synchronize();
      el("export-note").textContent = note + " Записано в аддон. Выполните /reload в игре.";
    } catch (exc) {
      el("export-note").textContent = note + " Передача не завершена. Нажмите «Синхронизировать с игрой» для повторной попытки. " + exc.message;
    }
  }
  el("local-sync").addEventListener("click", () => synchronize().catch(exc => error(exc.message)));
  function selectionChanged() {
    el("selected").textContent = `Выбрано: ${selected.size}`;
    const ready = status && status.published_run && !status.running && status.result_generation_matches && status.settings_match && status.strategy_versions_match && !status.observation_stale;
    el("export").disabled = !ready || !selected.size;
    let reason = "";
    if (!status) reason = "Обновите статус аналитики.";
    else if (status.running) reason = "Идёт пересчёт — дождитесь завершения.";
    else if (!status.published_run) reason = "Сначала нажмите «Пересчитать аналитику».";
    else if (status.observation_stale) reason = "Цены устарели: обновите данные и пересчитайте аналитику.";
    else if (!ready) reason = "Сохранённый расчёт не соответствует текущей базе, настройкам или правилам. Нажмите «Пересчитать аналитику».";
    else if (!selected.size) reason = "Отметьте нужную строку в столбце «В аддон».";
    el("export-block-reason").textContent = reason;
    el("export").title = reason || "Создать пакет из выбранных возможностей";
    el("export-form").hidden = true;
  }
  const clear = () => {el("results").replaceChildren(); el("detail").hidden = true; el("result-note").textContent = "";};
  const error = value => {el("error").textContent = value;};
  const riskNames = {
    aggregated_prices_no_executable_lot:"Доступность конкретного лота не подтверждена",
    partial_coverage:"Покрытие рынка частичное",unknown_supply:"Объём предложения неизвестен",
    unknown_sales:"Оценка продаж отсутствует",stale_sales_estimate:"Оценка продаж устарела",
    daily_aggregate_price:"Цена является дневным агрегатом",source_quality_warnings:"Источник сообщает о пропусках или качестве данных",
    incomplete_source_coverage:"Часть исходных данных недоступна",historical_resale_price_unconfirmed:"Перепродажа по исторической цене не подтверждена",
    npc_stock_and_access_unconfirmed:"Запасы и доступ к NPC не подтверждены",
    insufficient_observed_material_supply:"Наблюдаемых материалов недостаточно для одного крафта",
    profession_skill_recipe_access_unconfirmed:"Нужны подходящие профессия, навык и доступный рецепт",
    material_depth_unknown:"Объём материалов по минимальной цене неизвестен",result_sale_unconfirmed:"Продажа результата крафта не подтверждена",
    result_tradeability_unconfirmed:"Данные о привязке результата отсутствуют — возможность передачи не подтверждена",
    cooldown_and_conversion_access_unconfirmed:"Откат преобразований и доступность всей цепочки не подтверждены",
    batch_plan_not_globally_optimized:"Партии округлены вверх; излишки не используются между ветвями цепочки"
  };
  const costs = {purchase:"Закупка",materials:"Материалы",auction_cut:"Комиссия AH",lost_deposits:"Потерянные залоги",
    relisting_costs:"Повторные выставления",price_slippage:"Изменение цены при покупке/продаже",crafting_time:"Время крафта",cooldown_time:"Время отката преобразований"};
  const rejectionNames = {
    missing_positive_buy_price:"Нет положительной цены покупки",stale_price:"Цена покупки устарела",
    no_observed_supply:"Нет наблюдаемого предложения",missing_vendor_sell_price:"Неизвестна цена продажи NPC",
    not_profitable:"Нет прибыли",not_profitable_after_cut:"Нет прибыли после комиссии AH",
    insufficient_history:"Недостаточно истории",stale_history:"История устарела",
    unstable_history:"Историческая цена нестабильна",recent_price_decline:"Недавнее снижение цены",
    discount_below_threshold:"Скидка меньше порога",result_missing_positive_buy_price:"Нет положительной цены результата",
    result_stale_price:"Цена результата устарела",result_no_observed_supply:"Нет наблюдаемого предложения результата",
    incomplete_recipe:"Рецепт неполный",untradeable_result:"Результат нельзя передать",
    missing_material_price_or_quantity:"Нет цены или количества материала",
    no_complete_craft_path:"Нет полной цепочки в заданных пределах глубины и количества крафтов",
    single_step_or_buy_is_cheaper:"Достаточно одного этапа или промежуточный материал дешевле купить",
    craft_graph_search_limit:"Достигнут предел поиска цепочки — результат не публикуется",
    liquidity_unknown_sales:"Ликвидность: оценка продаж или её дата отсутствуют",
    liquidity_stale_sales:"Ликвидность: оценка продаж устарела",
    liquidity_insufficient_sales_history:"Ликвидность: недостаточно дней с оценками продаж",
    liquidity_low_sales:"Ликвидность: дневные продажи ниже порога",
    liquidity_unknown_supply:"Ликвидность: предложение неизвестно",
    liquidity_high_supply:"Ликвидность: слишком много предложения относительно продаж",
    liquidity_large_batch:"Ликвидность: выход крафта слишком велик относительно продаж"
  };
  function renderDiagnostics(run) {
    const target = el("diagnostics"); target.replaceChildren();
    const note = text => {const p = document.createElement("p"); p.textContent = text; target.append(p);};
    if (!run) {note("Ещё не запускался."); return;}
    const diagnostics = run.diagnostics || {}, quality = diagnostics.quality || {};
    const metadata = quality.metadata_summary || {};
    const states = {running:"выполняется",completed:"завершён",failed:"ошибка",interrupted:"прерван"};
    note(`Расчёт ${states[run.status] || run.status}. Сигналов: ${run.signal_count}. Предметов с наблюдаемыми ценами: ${diagnostics.observed_items ?? 0}.`);
    if (run.error) note(run.error);
    if (metadata.record_count !== undefined) {
      note(`Записей об источниках: ${metadata.record_count}. С предупреждениями: ${metadata.records_with_warnings}. С пропусками: ${metadata.records_with_gaps}.`);
    }
    if (quality.recipe_selection) {
      const recipes = quality.recipe_selection;
      note(`Выбор рецептов: ${recipes.selected ?? 0} действующих по последним снимкам выбранного рынка; конфликтующих: ${recipes.ambiguous ?? 0}. Старые версии исключены.`);
    }
    const rejections = diagnostics.rejections || {}, signals = diagnostics.strategy_signals || {};
    for (const strategy of Object.keys(run.strategy_versions || {})) {
      const heading = document.createElement("h3");
      heading.textContent = `${names[strategy] || strategy} · сигналов: ${signals[strategy] ?? "ещё не рассчитано"}`;
      target.append(heading);
      const reasons = Object.entries(rejections[strategy] || {}).sort((a, b) => b[1] - a[1]);
      if (!reasons.length) {note("Причины отсева не зарегистрированы."); continue;}
      const wrap = document.createElement("div"), table = document.createElement("table"); wrap.className = "table-wrap";
      const header = document.createElement("tr");
      for (const text of ["Причина отсева", "Количество"]) {const th = document.createElement("th"); th.textContent = text; header.append(th);}
      table.append(header);
      for (const [reason, count] of reasons) {
        const row = document.createElement("tr"), label = document.createElement("td"), number = document.createElement("td");
        label.textContent = rejectionNames[reason] || reason; number.textContent = count;
        row.append(label, number); table.append(row);
      }
      wrap.append(table); target.append(wrap);
    }
    note("Отсутствие цен ограничивает оценку возможностей. Полная диагностика сохранена на сервере.");
  }
  function showDetail(detail) {
    el("reason").textContent = detail.reason;
    const target = el("inputs"); target.replaceChildren();
    const list = document.createElement("dl"); list.className = "details";
    const field = (label, value) => {
      const row = document.createElement("div"), title = document.createElement("dt"), text = document.createElement("dd");
      title.textContent = label; text.textContent = value === null || value === undefined ? "неизвестно" : String(value);
      row.append(title, text); list.append(row);
    };
    const price = detail.details.price;
    field("Исполнение", "indicative — конкретный лот не подтверждён");
    field("Наблюдение / расчёт", date(detail.observed_at) + " / " + date(detail.generated_at));
    field("Источник", detail.source); field("Давность самого старого входного наблюдения", detail.data_age_seconds + " сек");
    field("Наблюдаемый минимум / медиана", money(price.min_copper) + " / " + money(price.median_copper));
    field("Наблюдаемый объём / аукционы", (price.quantity ?? "неизвестно") + " / " + (price.auctions ?? "неизвестно"));
    field("Дневные продажи — оценка источника", price.sales_estimated_per_day);
    if (price.sales_observed_at) field("Дата оценки продаж", date(price.sales_observed_at));
    field("Источник оценки продаж", price.sales_source || price.source);
    field("Оценка спроса", "Данные источника, не подтверждённые сделки; срок и вероятность продажи неизвестны.");
    const liquidity = detail.details.liquidity;
    if (liquidity) {
      field("Фильтр ликвидности крафта", liquidity.enabled ? (liquidity.passed ? "Пройден" : "Не пройден") : "Выключен при расчёте");
      if (liquidity.enabled) {
        field("Консервативная оценка продаж/сутки", liquidity.conservative_sales_per_day);
        field("Дней с оценкой / окно", `${liquidity.covered_days} / ${liquidity.window_days}`);
        field("Предложение / дневные продажи (не срок продажи)", liquidity.supply_to_daily_sales);
        field("Выход крафта / дневные продажи, %", liquidity.batch_daily_sales_percent);
        field("Пороги отбора", `Продажи ≥ ${liquidity.thresholds.min_sales_per_day}/сутки; предложение/продажи ≤ ${liquidity.thresholds.max_supply_days}; партия ≤ ${liquidity.thresholds.max_batch_sales_percent}%; дней ≥ ${liquidity.thresholds.min_sales_days}; возраст ≤ ${liquidity.thresholds.max_sales_age_seconds / 3600} ч.`);
      }
    }
    field("Выход / количество", detail.quantity);
    field("Учитываемые затраты", detail.costs_included.map(c => costs[c] || c).join(", "));
    field("Не учтено в прибыли", detail.costs_excluded.map(c => costs[c] || c).join(", ") || "Нет дополнительных затрат в этом расчёте");
    if (detail.details.vendor_sell_copper !== undefined) field("Продажа NPC за единицу", money(detail.details.vendor_sell_copper));
    if (detail.details.auction_cut_copper !== undefined) field("Комиссия AH", money(detail.details.auction_cut_copper));
    const history = detail.details.history;
    if (history) {
      field("Исторический ориентир", money(history.copper));
      field("История: дней / точек / период", `${history.day_count} / ${history.sample_count} / ${history.span_days} дней`);
      field("Последнее историческое наблюдение", date(history.latest_at));
      field("Историческая волатильность", history.relative_volatility === null ? "неизвестно" : (Number(history.relative_volatility) * 100).toFixed(2) + "%");
      field("Недавняя медиана", money(history.recent_copper));
    }
    const recipe = detail.details.recipe;
    if (recipe) {
      field("Рецепт", recipe.name); field("Профессия / навык", (recipe.profession || "неизвестно") + " / " + (recipe.skill_required ?? "неизвестно"));
      field("Получение рецепта", recipe.acquisition); field("Диапазон выхода / используемый минимум", `${recipe.qty_min}–${recipe.qty_max} / ${recipe.qty_min}`);
      field("Версия рецепта / снимок источника", recipe.version_hash + " / " + date(recipe.source_generated_at || recipe.source_retrieved_at));
    }
    target.append(list);
    if (detail.details.steps) {
      const heading = document.createElement("h3"); heading.textContent = "Порядок изготовления"; target.append(heading);
      const chain = document.createElement("ol");
      for (const step of detail.details.steps) {
        const row = document.createElement("li"), recipe = step.recipe;
        row.textContent = `${recipe.name || recipe.spell_id}: крафтов ${step.batches}; выход ${step.output_quantity}, нужно ${step.required_quantity}, излишек ${step.surplus_quantity}. Профессия: ${recipe.profession || "неизвестно"}, навык: ${recipe.skill_required ?? "неизвестно"}; получение: ${recipe.acquisition || "неизвестно"}. Материалы: ${step.inputs.map(r => r.item_id + " × " + r.quantity).join(", ")}. Версия ${recipe.version_hash}; снимок ${date(recipe.source_generated_at || recipe.source_retrieved_at)}.`;
        chain.append(row);
      }
      target.append(chain);
      const note = document.createElement("p"); note.textContent = "Покупаются только материалы из таблицы ниже. Излишки не добавлены к прибыли; комиссия AH учтена при продаже конечного результата."; target.append(note);
    }
    if (detail.details.materials) {
      const wrap = document.createElement("div"), table = document.createElement("table"), header = document.createElement("tr");
      wrap.className = "table-wrap";
      for (const title of ["Материал", "Количество", "За единицу", "Стоимость", "Источник"]) {
        const cell = document.createElement("th"); cell.textContent = title; header.append(cell);
      }
      table.append(header);
      for (const material of detail.details.materials) {
        const row = document.createElement("tr");
        for (const value of [material.item_name || material.item_id, material.quantity, money(material.unit_price_copper),
                            money(material.cost_copper), material.source === "npc" ? "NPC (доступность не подтверждена)" : "Наблюдаемый минимум AH"]) {
          const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
        }
        table.append(row);
      }
      wrap.append(table); target.append(wrap);
    }
    const risks = document.createElement("ul");
    for (const flag of detail.risk_flags) {const row = document.createElement("li"); row.textContent = riskNames[flag] || flag; risks.append(row);}
    target.append(risks);
    el("detail").hidden = false; el("detail").open = true;
  }
  function renderStatus(value) {
    status = value;
    for (const id of ["market", "discount", "cut", "craft-graph", "craft-depth", "craft-liquid", "craft-sales", "craft-supply", "craft-batch", "craft-window", "craft-days", "craft-age", "save"]) el(id).disabled = value.running;
    el("recalculate").disabled = value.running || !value.ready || !value.active_market;
    const run = value.published_run;
    el("status").textContent = (value.running ? "Расчёт выполняется. " : "") +
      `Наблюдение: ${date(value.observed_at)}. Последний успешный расчёт: ${date(run && run.finished_at)}.` +
      (value.observation_stale ? " Исходные цены устарели." : "") +
      (run && !value.result_generation_matches ? " База обновилась: для новых результатов нажмите «Пересчитать»." : "") +
      (run && !value.settings_match ? " Настройки изменены: сохранённый расчёт использовал прежнюю конфигурацию." : "");
    if (run && value.strategy_versions_match === false) el("status").textContent += " Правила расчёта обновлены: нажмите «Пересчитать».";
    error(value.error || "");
    renderDiagnostics(value.latest_run);
    selectionChanged();
    if (!value.running && timer) {clearTimeout(timer); timer = null;}
  }
  async function loadResults(token = revision) {
    const request = ++listRevision;
    clear(); el("result-note").textContent = "Загрузка результатов…";
    const params = new URLSearchParams({sort:el("sort").value, limit:"50", offset:String(offset),
      min_profit:el("min-profit").value || "0", min_roi:el("min-roi").value || "0"});
    if (el("strategy").value) params.set("strategy", el("strategy").value);
    if (el("confidence").value) params.set("confidence", el("confidence").value);
    const value = await api("/analytics/opportunities?" + params);
    if (token !== revision || request !== listRevision || !settings || value.market !== settings.active_market) return;
    if (selectedRun !== value.run_id || selectedMarket !== value.market) {
      selected.clear(); selectedRun = value.run_id; selectedMarket = value.market; selectionChanged();
    }
    clear();
    for (const item of value.items) {
      const row = document.createElement("tr");
      const text = [`${item.item_name} · ${names[item.strategy_id] || item.strategy_id}`, money(item.required_capital_copper),
        money(item.reference_price_copper), money(item.potential_profit_copper), `${item.roi_percent}%`, item.rating,
        `${item.details.price.sales_estimated_per_day ?? "неизвестно"} · ${date(item.details.price.sales_observed_at)}${item.risk_flags.includes("stale_sales_estimate") ? " · устарела" : ""}`];
      for (const value of text) {const cell = document.createElement("td"); cell.textContent = value; row.append(cell);}
      const cell = document.createElement("td"), button = document.createElement("button");
      button.type = "button"; button.textContent = "Подробнее";
      button.addEventListener("click", async () => {
        const detailToken = revision;
        try {
          const detail = await api("/analytics/opportunities/" + encodeURIComponent(item.id));
          if (detailToken !== revision || detail.market !== settings.active_market) return;
          showDetail(detail);
        } catch (exc) {if (detailToken === revision) error(exc.message);}
      });
      cell.append(button); row.append(cell);
      const choice = document.createElement("td"), checkbox = document.createElement("input"); checkbox.type = "checkbox";
      checkbox.setAttribute("aria-label", "Передать в аддон: " + item.item_name); checkbox.checked = selected.has(item.id);
      checkbox.addEventListener("change", () => {if (checkbox.checked) selected.set(item.id,item); else selected.delete(item.id); selectionChanged();});
      choice.append(checkbox); row.append(choice); el("results").append(row);
    }
    el("result-note").textContent = value.run_id ?
      `${value.total} возможностей по фильтру. Покрытие частичное; сделки и продажи не подтверждены.` :
      "Для активного рынка ещё нет успешного расчёта. Отсутствие данных не означает отсутствие прибыльных сделок.";
    el("page").textContent = value.total ? `${offset + 1}–${Math.min(offset + 50, value.total)} / ${value.total}` : "0";
    el("prev").disabled = offset === 0; el("next").disabled = offset + 50 >= value.total;
  }
  async function refresh() {
    const token = ++revision;
    try {
      const [markets, current, value, bridge] = await Promise.all([api("/analytics/markets"), api("/analytics/settings"), api("/analytics/status"), api("/addon/local-sync/status")]);
      if (token !== revision) return;
      settings = current;
      renderLocalBridge(bridge);
      el("market").replaceChildren();
      const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "Выберите рынок"; el("market").append(placeholder);
      for (const market of markets.markets) {
        const option = document.createElement("option"); option.value = market.market; option.textContent = market.label || market.market; el("market").append(option);
      }
      if (current.active_market && !markets.markets.some(m => m.market === current.active_market)) {
        const option = document.createElement("option"); option.value = current.active_market; option.textContent = current.active_market + " · отсутствует в текущей базе"; el("market").append(option);
      }
      el("market").value = current.active_market || "";
      el("discount").value = current.config.discount_percent; el("cut").value = current.config.auction_cut_percent;
      el("craft-graph").checked = current.config.enabled_strategies.includes("craft_graph");
      el("craft-depth").value = current.config.strategy_settings.craft_graph?.max_depth ?? 5;
      el("craft-liquid").checked = current.config.craft_liquidity_enabled;
      el("craft-sales").value = current.config.craft_min_sales_per_day;
      el("craft-supply").value = current.config.craft_max_supply_days;
      el("craft-batch").value = current.config.craft_max_batch_sales_percent;
      el("craft-window").value = current.config.craft_sales_window_days;
      el("craft-days").value = current.config.craft_min_sales_days;
      el("craft-age").value = current.config.craft_sales_max_age_seconds / 3600;
      renderStatus(value);
      await loadResults(token);
      if (value.running) watch();
    } catch (exc) {if (token === revision) error(exc.message);}
  }
  function watch() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(async () => {
      timer = null;
      const token = revision;
      try {
        const value = await api("/analytics/status");
        if (token !== revision) return;
        renderStatus(value);
        if (value.running) watch(); else await loadResults(token);
      } catch (exc) {if (token === revision) error(exc.message);}
    }, 1000);
  }
  async function save() {
    if (!settings) return false;
    const token = ++revision; clear(); offset = 0;
    error("");
    try {
      const enabled = settings.config.enabled_strategies.filter(id => id !== "craft_graph");
      if (el("craft-graph").checked) enabled.push("craft_graph");
      const updated = await api("/analytics/settings", {method:"PUT", body:JSON.stringify({active_market:el("market").value || null,
        config:{...settings.config, discount_percent:el("discount").value, auction_cut_percent:el("cut").value,
          craft_liquidity_enabled:el("craft-liquid").checked, craft_min_sales_per_day:el("craft-sales").value,
          craft_max_supply_days:el("craft-supply").value, craft_max_batch_sales_percent:el("craft-batch").value,
          craft_sales_window_days:Number(el("craft-window").value), craft_min_sales_days:Number(el("craft-days").value),
          craft_sales_max_age_seconds:Math.round(Number(el("craft-age").value) * 3600),
          enabled_strategies:enabled, strategy_settings:{...settings.config.strategy_settings,
            craft_graph:{...settings.config.strategy_settings.craft_graph, max_depth:Number(el("craft-depth").value)}}}})});
      if (token !== revision) return false;
      settings = updated; await refresh(); return true;
    } catch (exc) {if (token === revision) {await refresh(); error(exc.message);} return false;}
  }
  el("settings").addEventListener("submit", event => {event.preventDefault(); save();});
  el("market").addEventListener("change", save);
  el("refresh").addEventListener("click", refresh);
  el("recalculate").addEventListener("click", async () => {
    error(""); el("recalculate").disabled = true;
    try {
      if (!await save()) return;
      if (!settings || settings.active_market !== el("market").value) return;
      await api("/analytics/recalculate", {method:"POST"});
      await refresh();
    } catch (exc) {error(exc.message); el("recalculate").disabled = Boolean(status && status.running);}
  });
  el("filters").addEventListener("submit", event => {event.preventDefault(); offset = 0; loadResults().catch(exc => error(exc.message));});
  el("prev").addEventListener("click", () => {offset = Math.max(0, offset - 50); loadResults().catch(exc => error(exc.message));});
  el("next").addEventListener("click", () => {offset += 50; loadResults().catch(exc => error(exc.message));});
  el("export").addEventListener("click", () => {
    const target = el("export-limits"); target.replaceChildren();
    const limits = new Map(); let budget = 0;
    for (const item of selected.values()) {
      budget += item.required_capital_copper;
      const lines = ["simple_craft", "craft_graph"].includes(item.strategy_id) ? item.details.materials :
        [{item_id:item.item_id,item_name:item.item_name,source:"auction",unit_price_copper:item.buy_price_copper}];
      for (const line of lines) if (line.source === "auction") {
        const old = limits.get(line.item_id);
        if (!old || line.unit_price_copper < old.price) limits.set(line.item_id,{name:line.item_name || String(line.item_id),price:line.unit_price_copper});
      }
    }
    for (const [id, line] of limits) {
      const label = document.createElement("label"), input = document.createElement("input");
      label.textContent = `${line.name}: максимум за единицу, copper`; input.type = "number"; input.min = "1"; input.max = "9007199254740991";
      input.required = true; input.value = line.price; input.dataset.itemId = String(id); label.append(input); target.append(label);
    }
    el("export-budget").value = budget; el("export-form").hidden = false;
    el("export-note").textContent = "Пакет ещё не создан. Проверьте цены и бюджет в открытой форме, затем нажмите «Создать пакет для аддона».";
    el("export-form").scrollIntoView({behavior:"smooth",block:"start"});
  });
  el("export-form").addEventListener("submit", async event => {
    event.preventDefault(); error("");
    const submit = el("export-form").querySelector("button[type='submit']");
    if (submit.disabled) return;
    submit.disabled = true;
    const limits = {};
    for (const input of el("export-limits").querySelectorAll("input")) limits[input.dataset.itemId] = Number(input.value);
    try {
      const result = await api("/addon/exports", {method:"POST",body:JSON.stringify({request_id:crypto.randomUUID(),
        opportunity_ids:Array.from(selected.keys()),unit_limits:limits,max_budget_copper:Number(el("export-budget").value)})});
      await deliverPackage(`Пакет создан: ${result.tasks.length} заданий. Срок пакета: ${date(result.expires_at)}.`);
      selected.clear(); selectionChanged(); await loadResults();
    } catch (exc) {error(exc.message);} finally {submit.disabled = false;}
  });
  el("game-history").addEventListener("click", async () => {
    try {
      const value = await api("/addon/history"); const target = el("game-events"); target.replaceChildren();
      for (const event of value.events) {
        const p = document.createElement("p"); p.textContent = `${date(event.observed_at)} · ${event.kind}/${event.state} · ${event.item_id || "задание"} × ${event.quantity} · ${money(event.cost_copper)}`; target.append(p);
      }
      if (!value.events.length) target.textContent = "Игровые отчёты ещё не синхронизированы для активного рынка.";
    } catch (exc) {error(exc.message);}
  });
  el("renew-packages").addEventListener("click", async () => {
    try {
      const current = await api("/addon/packages?include_expired=true&market=" + encodeURIComponent(settings.active_market));
      for (const p of current.packages) await api("/addon/packages/" + p.package_id + "/renew", {method:"POST"});
      await deliverPackage(`Продлено пакетов: ${current.packages.length}. Купленные количества и идентификаторы заданий сохраняются.`);
    } catch (exc) {error(exc.message);}
  });
  window.addEventListener("cennik-connected", refresh);
  window.addEventListener("cennik-data-updated", refresh);
  window.addEventListener("cennik-disconnected", () => {revision++; clearTimeout(timer); timer = null; clear(); settings = null; selected.clear(); selectionChanged(); renderLocalBridge({available:false});});
})();
