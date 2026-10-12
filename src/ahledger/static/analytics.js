"use strict";
(() => {
  const el = id => document.getElementById("analytics-" + id);
  const names = {vendor_arbitrage:"Vendor Arbitrage", historical_discount:"Historical Discount", simple_craft:"Simple Craft Arbitrage"};
  const date = value => value ? new Date(value).toLocaleString("ru-RU") : "неизвестно";
  const money = value => {
    if (value === null || value === undefined) return "неизвестно";
    const copper = BigInt(value);
    return `${copper / 10000n} з ${copper % 10000n / 100n} с ${copper % 100n} м`;
  };
  let settings = null, status = null, offset = 0, revision = 0, listRevision = 0, timer = null;
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
    result_tradeability_unconfirmed:"Данные о привязке результата отсутствуют — возможность передачи не подтверждена"
  };
  const costs = {purchase:"Закупка",materials:"Материалы",auction_cut:"Комиссия AH",lost_deposits:"Потерянные залоги",
    relisting_costs:"Повторные выставления",price_slippage:"Изменение цены при покупке/продаже",crafting_time:"Время крафта"};
  const rejectionNames = {
    missing_positive_buy_price:"Нет положительной цены покупки",stale_price:"Цена покупки устарела",
    no_observed_supply:"Нет наблюдаемого предложения",missing_vendor_sell_price:"Неизвестна цена продажи NPC",
    not_profitable:"Нет прибыли",not_profitable_after_cut:"Нет прибыли после комиссии AH",
    insufficient_history:"Недостаточно истории",stale_history:"История устарела",
    unstable_history:"Историческая цена нестабильна",recent_price_decline:"Недавнее снижение цены",
    discount_below_threshold:"Скидка меньше порога",result_missing_positive_buy_price:"Нет положительной цены результата",
    result_stale_price:"Цена результата устарела",result_no_observed_supply:"Нет наблюдаемого предложения результата",
    incomplete_recipe:"Рецепт неполный",untradeable_result:"Результат нельзя передать",
    missing_material_price_or_quantity:"Нет цены или количества материала"
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
    }
    target.append(list);
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
    for (const id of ["market", "discount", "cut", "save"]) el(id).disabled = value.running;
    el("recalculate").disabled = value.running || !value.ready || !value.active_market;
    const run = value.published_run;
    el("status").textContent = (value.running ? "Расчёт выполняется. " : "") +
      `Наблюдение: ${date(value.observed_at)}. Последний успешный расчёт: ${date(run && run.finished_at)}.` +
      (value.observation_stale ? " Исходные цены устарели." : "") +
      (run && !value.result_generation_matches ? " База обновилась: для новых результатов нажмите «Пересчитать»." : "") +
      (run && !value.settings_match ? " Настройки изменены: сохранённый расчёт использовал прежнюю конфигурацию." : "");
    error(value.error || "");
    renderDiagnostics(value.latest_run);
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
    clear();
    for (const item of value.items) {
      const row = document.createElement("tr");
      const text = [`${item.item_name} · ${names[item.strategy_id] || item.strategy_id}`, money(item.required_capital_copper),
        money(item.reference_price_copper), money(item.potential_profit_copper), `${item.roi_percent}%`, item.rating];
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
      cell.append(button); row.append(cell); el("results").append(row);
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
      const [markets, current, value] = await Promise.all([api("/analytics/markets"), api("/analytics/settings"), api("/analytics/status")]);
      if (token !== revision) return;
      settings = current;
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
      const updated = await api("/analytics/settings", {method:"PUT", body:JSON.stringify({active_market:el("market").value || null,
        config:{...settings.config, discount_percent:el("discount").value, auction_cut_percent:el("cut").value}})});
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
  window.addEventListener("cennik-connected", refresh);
  window.addEventListener("cennik-disconnected", () => {revision++; clearTimeout(timer); timer = null; clear(); settings = null;});
})();
