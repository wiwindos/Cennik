local _, C = ...
local U = {section="Opportunities",selected={}}
C.UI = U
U.pool,U.used = {},{}
function U:Widget(kind,parent,template,role)
  local key=kind .. ":" .. (template or "") .. ":" .. (role or "")
  local pool=self.pool[key] or {}; self.pool[key]=pool
  local widget=table.remove(pool)
  if not widget then
    widget=kind=="FontString" and parent:CreateFontString(nil,"OVERLAY",template) or CreateFrame(kind,nil,parent,template)
  else widget:SetParent(parent); widget:ClearAllPoints() end
  widget:Show()
  table.insert(self.used,{key=key,widget=widget})
  return widget
end
local function money(value)
  if value==nil then return "неизвестно" end
  return (value<0 and "−" or "") .. GetMoneyString(math.abs(value),true)
end
local function percent(value) return value and string.format("%.2f%%",value) or "неизвестно" end
local states={pending_scan="Нужен скан",scanning="Сканирование",scan_failed="Скан не завершён",
  requires_manual_check="Нужна проверка условий",insufficient_supply="Недостаточно товара",limits_exceeded="Нужно изменить лимиты",
  not_profitable="Невыгодно",ready_to_buy="Можно закупать",partially_bought="Закуплено частично",completed="Закупка завершена",cancelled="Отменено"}
local strategies={vendor_arbitrage="Торговля с NPC",simple_craft="Крафт",craft_graph="Цепочка крафта",historical_discount="Историческая скидка"}
local risks={aggregated_prices_no_executable_lot="Серверная цена не подтверждала конкретный лот",partial_coverage="Частичное покрытие рынка",
  material_depth_unknown="Сервер не знал объём материалов по минимальной цене",profession_skill_recipe_access_unconfirmed="Доступ к профессии и рецепту не подтверждён",
  result_sale_unconfirmed="Продажа готового товара не подтверждена",result_tradeability_unconfirmed="Передаваемость результата не подтверждена",
  source_quality_warnings="Предупреждения о качестве источника",unknown_supply="Предложение неизвестно",unknown_sales="Оценка продаж отсутствует",
  stale_sales_estimate="Серверная оценка продаж устарела",npc_stock_and_access_unconfirmed="Доступ к NPC и его запасам не подтверждён",
  insufficient_observed_material_supply="Сервер наблюдал недостаточно материалов",historical_resale_price_unconfirmed="Перепродажа по исторической цене не подтверждена",
  cooldown_and_conversion_access_unconfirmed="Откаты и доступ к преобразованиям не подтверждены",batch_plan_not_globally_optimized="Излишки не распределены между ветвями цепочки",
  daily_aggregate_price="Серверная цена — дневной агрегат",incomplete_source_coverage="Часть исходных данных отсутствует",stale_price="Серверная цена устарела"}
function U:Live(task)
  local value=task.validation
  if not value or not value.complete or not value.checked_at or C:Now()-value.checked_at>90 then return nil end
  local ok=C:CheckTask(task,C.db.pending and C.db.pending.task_id==task.task_id)
  if not ok then return nil end
  for _,scan in pairs(task.scans) do if not C.Validator:Fresh(scan) then return nil end end
  for _,line in ipairs(task.lines) do if line.source=="npc" and not C.NPC:Quote(task,line.item_id) then return nil end end
  for _,material in ipairs(value.materials or {}) do
    if material.source=="npc" then
      local quote=C.NPC:Quote(task,material.item_id)
      if not quote or not material.quote or quote.batch_quantity~=material.quote.batch_quantity or quote.batch_price_copper~=material.quote.batch_price_copper
        or (quote.available and quote.available<material.needed) then return nil end
    end
  end
  return value
end
local function label(parent,text,x,y,width)
  local font=U:Widget("FontString",parent,"GameFontHighlightSmall")
  font:SetPoint("TOPLEFT",parent,"TOPLEFT",x,y); font:SetWidth(width or 620); font:SetJustifyH("LEFT"); font:SetText(text)
  return font
end
local function button(parent,text,x,y,width,callback)
  local b=U:Widget("Button",parent,"UIPanelButtonTemplate")
  b:Enable(); b:SetScript("OnEnter",nil); b:SetScript("OnLeave",nil)
  b:SetSize(width or 120,24); b:SetPoint("TOPLEFT",parent,"TOPLEFT",x,y); b:SetText(text); b:SetScript("OnClick",callback)
  return b
end
local function edit(parent,x,y,width,value)
  local e=U:Widget("EditBox",parent,"InputBoxTemplate")
  e:SetSize(width or 100,22); e:SetPoint("TOPLEFT",parent,"TOPLEFT",x,y); e:SetAutoFocus(false); e:SetText(tostring(value or ""))
  return e
end
function U:Task()
  local task=self.focus and C.db.tasks[self.focus]
  if task and not task.hidden and task.market==C.db.current_market and task.run_id==C.db.current_run then return task end
end
function U:Show()
  local lib=LibStub and LibStub("LibAHTab-1-0",true)
  if lib and self.frame then lib:SetSelected("Cennik") end
end
function U:QueueRefresh()
  if self.refreshQueued then return end
  self.refreshQueued=true
  C_Timer.After(0,function()
    U.refreshQueued=false
    if U.frame and U.frame:IsShown() then U:Refresh() end
  end)
end
function U:SelectSection(section)
  if C.Sniper and C.Sniper.ClosePopups then C.Sniper:ClosePopups() end
  self.section=section; self.resetScroll=true
  self:Refresh(); self:QueueRefresh()
end
function U:BindingStatus()
  local faction=string.lower(UnitFactionGroup("player") or "")
  local character=UnitName("player") .. " · " .. GetRealmName() .. " · " .. (faction=="alliance" and "Альянс" or faction=="horde" and "Орда" or faction)
  return (C:MarketBindingMatches(C.db.current_market) and "Рынок подтверждён: " or "Подтвердите рынок для: ") .. character
end
function U:Attach()
  if C.Sniper and C.Sniper.WatchAuctionTabs then C.Sniper:WatchAuctionTabs() end
  if self.frame then self:Refresh(); return end
  local lib=LibStub and LibStub("LibAHTab-1-0",true)
  if C.Adapter.error or not lib then print("Cennik: " .. (C.Adapter.error or "Auctionator не загрузил LibAHTab")); return end
  local frame=CreateFrame("Frame",nil,AuctionHouseFrame)
  self.frame=frame; frame:SetPoint("TOPLEFT",AuctionHouseFrame,"TOPLEFT",16,-62); frame:SetSize(690,500)
  self.note=frame:CreateFontString(nil,"OVERLAY","GameFontHighlightSmall"); self.note:SetPoint("TOPLEFT",0,-2); self.note:SetWidth(660)
  self.bindingNote=frame:CreateFontString(nil,"OVERLAY","GameFontHighlightSmall"); self.bindingNote:SetPoint("TOPLEFT",0,-20); self.bindingNote:SetWidth(660)
  self.noticeNote=frame:CreateFontString(nil,"OVERLAY","GameFontHighlightSmall"); self.noticeNote:SetPoint("TOPLEFT",0,-38); self.noticeNote:SetWidth(660)
  self.tabs={}
  for i,name in ipairs({"Opportunities","Buy Queue","History"}) do
    local b=CreateFrame("Button",nil,frame,"UIPanelButtonTemplate"); b:SetSize(i==3 and 90 or 125,24); b:SetPoint("TOPLEFT",(i-1)*130,-62); b:SetText(({Opportunities="Возможности",["Buy Queue"]="Очередь покупок",History="История"})[name])
    self.tabs[name]=b
    b:SetScript("OnClick",function() U:SelectSection(name) end)
  end
  local bind=CreateFrame("Button",nil,frame,"UIPanelButtonTemplate"); bind:SetSize(150,24); bind:SetPoint("TOPLEFT",355,-62); bind:SetText("Подтвердить рынок")
  self.bindButton=bind
  bind:SetScript("OnClick",function()
    local ok,reason=C:BindMarket(C.db.current_market)
    U.notice=ok and U:BindingStatus() or ("Не удалось подтвердить рынок: " .. reason)
    U:Refresh()
    if not ok then print("Cennik: " .. reason) end
  end)
  local market=CreateFrame("Button",nil,frame,"UIPanelButtonTemplate")
  market:SetSize(75,24); market:SetPoint("TOPLEFT",510,-62); market:SetText("Рынок")
  market:SetScript("OnClick",function() U:SelectSection("Market") end)
  local sniper=CreateFrame("Button",nil,frame,"UIPanelButtonTemplate")
  sniper:SetSize(90,24); sniper:SetPoint("TOPLEFT",590,-62); sniper:SetText("Снайпер")
  sniper:SetScript("OnClick",function() U:SelectSection("Sniper") end)
  self.body=CreateFrame("Frame",nil,frame); self.body:SetPoint("TOPLEFT",0,-100); self.body:SetSize(670,375)
  self.body:SetClipsChildren(true)
  frame:SetScript("OnShow",function() U.resetScroll=true; U:Refresh(); U:QueueRefresh() end)
  lib:CreateTab("Cennik",frame,"Cennik","Cennik / Auctionator")
  self:Refresh()
end
function U:Clear()
  for _, entry in ipairs(self.used) do
    entry.widget:Hide()
    table.insert(self.pool[entry.key],entry.widget)
  end
  self.used={}
end
function U:Details(task)
  local o=task.opportunity
  local live=self:Live(task)
  local text={C.Items:Link(task.item_id) .. " (#" .. task.item_id .. ") / " .. (strategies[task.strategy_id] or task.strategy_id),
    "Проверка: " .. (states[task.state] or task.state) .. "; " .. (task.reason or "")}
  if live then
    table.insert(text,"ТЕКУЩАЯ ОЦЕНКА: " .. date("%H:%M:%S",live.checked_at) .. "; данные AH действуют до 90 секунд")
    table.insert(text,"Все затраты: " .. money(live.cost_copper) .. "; осталось AH: " .. money(live.remaining_cost_copper) .. "; NPC: " .. money(live.npc_cost_copper))
    table.insert(text,"Выручка: " .. money(live.gross_revenue_copper) .. "; комиссия: " .. money(live.fee_copper) .. "; после комиссии: " .. money(live.net_revenue_copper))
    table.insert(text,"Потенциальная прибыль: " .. money(live.profit_copper) .. "; ROI: " .. percent(live.roi_percent) .. "; маржа от выручки: " .. percent(live.margin_percent))
    table.insert(text,"Безубыточная цена за штуку: " .. money(live.break_even_unit_copper) .. "; " .. live.revenue_basis)
  else table.insert(text,"Полная текущая экономика неизвестна. Требуются свежие цены и достаточное количество всех материалов; повторите проверку.") end
  if live then table.insert(text,"Для сравнения — расчёт из веба: затраты " .. money(o.required_capital_copper) .. "; прибыль " .. money(task.predicted_profit_copper) .. "; ROI " .. tostring(o.roi_percent) .. "%")
  else table.insert(text,"Исходный расчёт веба не используется как текущая прибыль. Сначала нужна полная проверка.") end
  local price=o.details.price or {}
  table.insert(text,"Продажи/сутки — оценка источника: " .. tostring(price.sales_estimated_per_day or "неизвестно") .. "; дата: " .. tostring(price.sales_observed_at or "неизвестно") .. "; источник: " .. tostring(price.sales_source or price.source or "неизвестно"))
  table.insert(text,"Оценка спроса не подтверждает сделки и не гарантирует быструю продажу.")
  local liquidity=o.details.liquidity
  if liquidity and liquidity.enabled then
    local basis=liquidity.method=="latest_source" and "последняя оценка источника" or ("история: дней " .. tostring(liquidity.covered_days) .. "/" .. tostring(liquidity.window_days))
    table.insert(text,"Фильтр ликвидности: пройден при расчёте; " .. basis .. "; оценка " .. tostring(liquidity.conservative_sales_per_day) .. "/сутки; предложение/продажи " .. tostring(liquidity.supply_to_daily_sales) .. " (не срок продажи)")
  end
  table.insert(text,"Покупки / NPC:")
  local recipe=o.details.recipe
  if recipe then table.insert(text,"Рецепт: " .. (recipe.result_item_id and C.Items:Name(recipe.result_item_id) or ("#" .. tostring(recipe.spell_id))) .. "; профессия " .. tostring(recipe.profession) .. "; навык " .. tostring(recipe.skill_required) .. "; получение " .. tostring(recipe.acquisition)) end
  for _, line in ipairs(task.lines) do
    local availability="NPC: текущая цена неизвестна; откройте торговца или введите цену"
    if line.source=="auction" then
      local scan=task.scans and task.scans[line.item_id]
      if scan and scan.complete and C:Now()-scan.observed_at<=90 then
        local needed=math.max(0,line.quantity-(task.bought[line.item_id] or 0))
        local plan,_,reason=C.Validator:Plan(task,line,needed,true)
        availability=plan and ("AH: нужно " .. needed .. ", закупка " .. plan.quantity .. " шт. за " .. money(plan.cost) .. "; максимум/шт. " .. money(plan.max_unit_price) .. (plan.extra_quantity>0 and ("; лишних " .. plan.extra_quantity) or "")) or (reason or "Не проверено")
      else availability="AH: цена и достаточное количество ещё не проверены" end
    else
      local quote=C.NPC:Quote(task,line.item_id)
      if quote then availability="NPC: " .. money(quote.batch_price_copper) .. " за " .. quote.batch_quantity .. " шт.; " .. quote.source .. (quote.from_database and "; стабильная цена из исходных данных" or ("; " .. date("%H:%M:%S",quote.observed_at) .. "; цена действительна 15 минут")) end
    end
    table.insert(text,C.Items:Link(line.item_id) .. " (#" .. line.item_id .. ") × " .. line.quantity .. "; предел " .. money(line.unit_limit_copper) .. "; " .. availability)
  end
  local translated={}
  for _,flag in ipairs(o.risk_flags or {}) do translated[#translated+1]=risks[flag] or flag end
  table.insert(text,"Ограничения исходного расчёта: " .. table.concat(translated,"; "))
  for index,step in ipairs(o.details.steps or {}) do
    table.insert(text,index .. ". " .. (step.recipe.result_item_id and C.Items:Name(step.recipe.result_item_id) or ("Рецепт #" .. tostring(step.recipe.spell_id))) .. "; крафтов " .. step.batches .. "; версия " .. step.recipe.version_hash .. "; профессия " .. tostring(step.recipe.profession) .. "; навык " .. tostring(step.recipe.skill_required) .. "; излишек " .. step.surplus_quantity)
  end
  for item,scan in pairs(task.scans or {}) do
    table.insert(text,"Скан " .. C.Items:Name(item) .. " (#" .. item .. "); " .. scan.observed_at .. "; полный: " .. tostring(scan.complete) .. "; лотов: " .. #scan.rows)
    for index,row in ipairs(scan.rows) do
      if index>25 then table.insert(text,"… дополнительные лоты сохранены в скане"); break end
      table.insert(text,"  доступно " .. (row.available_quantity or row.quantity) .. " шт. × " .. money(row.unit_price) .. "/шт." .. (row.commodity and "; нужное количество через котировку" or "; одна покупка = 1 шт.") .. (row.vendor_known and ("; продажа NPC/шт. " .. money(row.vendor_sell_copper) .. "; " .. row.vendor_source) or ""))
    end
  end
  return table.concat(text,"\n")
end
function U:AttachScrollChild(scroll,child)
  local offset=(self.resetScroll or scroll.cennikLayout~=true) and 0 or scroll:GetVerticalScroll()
  scroll:SetScrollChild(child)
  -- Reused widgets have lost anchors; assigning the same scroll child can be a no-op.
  child:ClearAllPoints()
  child:SetPoint("TOPLEFT",scroll,"TOPLEFT",0,0)
  child:Show()
  scroll:UpdateScrollChildRect()
  scroll:SetVerticalScroll(math.min(offset,math.max(0,scroll:GetVerticalScrollRange())))
  scroll.cennikLayout=true
end
function U:Text(text,x,y,width,height)
  local role="text-" .. self.section
  local scroll=U:Widget("ScrollFrame",self.body,"UIPanelScrollFrameTemplate",role)
  scroll:SetClipsChildren(true)
  scroll:SetPoint("TOPLEFT",x,y); scroll:SetSize(width,height)
  -- FontString scroll content is clipped; multiline EditBox text can escape its viewport.
  local box=U:Widget("Frame",scroll,nil,role)
  local content=U:Widget("FontString",box,"GameFontHighlightSmall",role .. "-content")
  content:SetPoint("TOPLEFT",0,0); content:SetWidth(width); content:SetHeight(0)
  content:SetJustifyH("LEFT"); content:SetJustifyV("TOP"); content:SetText(text)
  local contentHeight=content:GetStringHeight()+16
  content:SetHeight(contentHeight)
  box:SetSize(width,math.max(height,contentHeight))
  box.text=text
  self:AttachScrollChild(scroll,box)
  return box
end
function U:Opportunities()
  button(self.body,"Проверить выбранные",0,0,180,function()
    local tasks={}
    for id in pairs(U.selected) do
      local task=C.db.tasks[id]
      if task and not task.hidden and task.market==C.db.current_market and task.run_id==C.db.current_run then table.insert(tasks,task) end
    end
    if #tasks==0 then U.notice="Отметьте задания галочками слева или нажмите «Проверить» в подробностях"; U:Refresh(); return end
    table.sort(tasks,function(a,b) return a.task_id<b.task_id end)
    local index=0
    local function nextTask()
      index=index+1
      if tasks[index] then
        local started=C.Scanner:Start(tasks[index],nextTask)
        if not started then C_Timer.After(0,nextTask) end
      end
    end
    nextTask()
  end)
  button(self.body,"Скрытые задания",185,0,145,function() U:SelectSection("Hidden") end)
  local rows={}
  for _, task in pairs(C.db.tasks) do
    if not task.hidden and task.market==C.db.current_market and task.run_id==C.db.current_run then table.insert(rows,task) end
  end
  table.sort(rows,function(a,b) return a.task_id<b.task_id end)
  self.opportunityCount=#rows
  label(self.body,"Заданий: " .. #rows .. ". Нажмите строку.",340,-6,290)
  local listHeight=math.min(120,math.max(56,math.floor(self.body:GetHeight()*0.30)))
  local actionY=listHeight+40
  local scroll=U:Widget("ScrollFrame",self.body,"UIPanelScrollFrameTemplate","opportunity-list")
  scroll:SetSize(630,listHeight); scroll:SetPoint("TOPLEFT",0,-30)
  local list=U:Widget("Frame",scroll,nil,"opportunity-list"); list:SetSize(630,listHeight)
  self.opportunityScroll,self.opportunityList,self.opportunityRows=scroll,list,{}
  local y=0
  for index,task in ipairs(rows) do
    local check=U:Widget("CheckButton",list,"UICheckButtonTemplate"); check:SetPoint("TOPLEFT",0,y-4); check:SetChecked(self.selected[task.task_id])
    check:SetScript("OnClick",function() U.selected[task.task_id]=check:GetChecked() or nil end)
    local live=U:Live(task)
    local summary=(strategies[task.strategy_id] or task.strategy_id) .. " · " .. (live and ("Прибыль: " .. money(live.profit_copper)) or "нет текущей оценки") .. " · " .. (states[task.state] or task.state)
    local row=button(list,"",32,y,502,function() U.focus=task.task_id; U:Refresh() end)
    local title=U:Widget("FontString",row,"GameFontHighlight","opportunity-title")
    title:SetPoint("TOPLEFT",10,-5); title:SetWidth(482); title:SetHeight(0)
    title:SetJustifyH("LEFT"); title:SetWordWrap(true); title:SetNonSpaceWrap(true); title:SetText(C.Items:Label(task.item_id))
    local titleHeight=math.max(18,title:GetStringHeight()); title:SetHeight(titleHeight)
    local detail=U:Widget("FontString",row,"GameFontHighlightSmall","opportunity-summary")
    detail:SetPoint("TOPLEFT",10,-titleHeight-8); detail:SetWidth(482); detail:SetHeight(0)
    detail:SetJustifyH("LEFT"); detail:SetWordWrap(true); detail:SetNonSpaceWrap(true); detail:SetText(summary)
    local detailHeight=detail:GetStringHeight(); detail:SetHeight(detailHeight)
    local rowHeight=math.max(46,titleHeight+detailHeight+14); row:SetHeight(rowHeight)
    row.cennikTitle,row.cennikSummary=title,detail
    button(list,"Скрыть",540,y-4,85,function()
      local ok,reason=C:HideTask(task)
      if ok then U.selected[task.task_id]=nil; if U.focus==task.task_id then U.focus=nil end end
      U.notice=ok and "Задание перенесено в скрытые" or reason; U:Refresh()
    end)
    self.opportunityRows[#self.opportunityRows+1]=row
    row:SetScript("OnEnter",function(self)
      local info=C.Items:Get(task.item_id)
      if GameTooltip then
        GameTooltip:SetOwner(self,"ANCHOR_RIGHT")
        if info and info.link then GameTooltip:SetHyperlink(info.link) else GameTooltip:SetText(C.Items:Name(task.item_id)) end
        GameTooltip:AddLine(summary,1,1,1,true)
        if task.reason and task.reason~="" then GameTooltip:AddLine(task.reason,1,0.82,0,true) end
        GameTooltip:Show()
      end
    end)
    row:SetScript("OnLeave",function() if GameTooltip then GameTooltip:Hide() end end)
    y=y-rowHeight-6
  end
  list:SetHeight(math.max(listHeight,-y))
  if #rows==0 then label(list,"Нет заданий для текущего рынка и расчёта. Синхронизируйте пакет на странице и выполните /reload.",8,-12,600) end
  self:AttachScrollChild(scroll,list)
  scroll:Show()
  local task=self:Task()
  if task then
    button(self.body,"Проверить",0,-actionY,110,function() C.Scanner:Start(task) end)
    button(self.body,"В очередь",115,-actionY,110,function() local _,reason=C.BuyQueue:Add(task); if reason then U.notice=reason; U:Refresh() end end)
    button(self.body,"Поиск",230,-actionY,110,function()
      local ok,reason=C.Adapter:Shopping(task.needs_variant_item_id or task.item_id)
      if not ok then U.notice=reason; U:Refresh() end
    end)
    button(self.body,"Выбрать вариант",345,-actionY,155,function()
      local key=C.lastSelectedKey
      if C.db.pending then U.notice="Сначала завершите или уточните результат предыдущей покупки"; U:Refresh(); return end
      if not key then U.notice="Сначала откройте предложения нужного предмета в поиске Auctionator"; U:Refresh(); return end
      local permitted=key.itemID==task.item_id
      for _, line in ipairs(task.lines) do if line.source=="auction" and line.item_id==key.itemID then permitted=true end end
      if not permitted then U.notice="Выбран другой предмет (#" .. tostring(key.itemID) .. "); его нет в этом задании"; U:Refresh(); return end
      task.selected_keys[key.itemID]=C:Clone(key)
      task.validation=nil; task.scans={}; C.BuyQueue.ticket=nil
      U.notice="Вариант подтверждён: " .. C.Items:Name(key.itemID) .. " (#" .. key.itemID .. ")"
      C:State(task,"pending_scan","Вариант подтверждён игроком; повторите проверку")
    end)
    local conditions=U:Widget("CheckButton",self.body,"UICheckButtonTemplate")
    conditions:SetPoint("TOPLEFT",0,-actionY-28); conditions:SetChecked(task.conditions_confirmed)
    label(self.body,"Профессия, рецепт, NPC проверены",32,-actionY-34,280)
    conditions:SetScript("OnClick",function() task.conditions_confirmed=conditions:GetChecked(); task.validation=nil; C.BuyQueue.ticket=nil; C:State(task,"pending_scan","Повторите проверку цен") end)
    button(self.body,"Скрыть задание",470,-actionY-28,155,function()
      local ok,reason=C:HideTask(task)
      if ok then U.selected[task.task_id]=nil; U.focus=nil; U.notice="Задание скрыто. История и покупки сохранены."
      else U.notice=reason end
      U:Refresh()
    end)
    button(self.body,"Принять цены",320,-actionY-28,145,function()
      if C.db.pending or not U:Live(task) then U.notice="Сначала выполните полную свежую проверку"; U:Refresh(); return end
      U.limitTask=task.task_id; U.limitUseCurrent=true; U.limitDraft=nil; U.section="Limits"; U:Refresh()
    end)
    button(self.body,"Предмет / снайпер / рецепт",0,-actionY-62,310,function() C.Sniper:ShowCard(task.item_id) end)
    local limits=button(self.body,"Цены / лимиты",505,-actionY,120,function()
      U.limitTask=task.task_id; U.limitUseCurrent=false; U.limitDraft=nil; U.section="Limits"; U:Refresh()
    end)
    limits:SetEnabled(not C.db.pending)
    self:Text(self:Details(task),0,-actionY-92,625,math.max(40,self.body:GetHeight()-actionY-104))
  else label(self.body,"Выберите задание. Пакеты загружаются через Bridge и /reload.",0,-165) end
end
function U:Queue()
  local task=C.BuyQueue:Task()
  button(self.body,"Подготовить / обновить",0,0,180,function() C.db.queue_stopped=false; C.BuyQueue:Prepare() end)
  local pending=C.db.pending
  local buy=button(self.body,pending and pending.stage=="quoted" and "Подтвердить" or "Купить",185,0,110,function()
    if C.db.pending and C.db.pending.stage=="quoted" then C.Execution:Confirm() else C.Execution:Start() end
  end)
  buy:SetEnabled((pending and pending.stage=="quoted") or (C.BuyQueue.ticket~=nil and not pending))
  button(self.body,"Пропустить",300,0,110,function() C.BuyQueue:Skip(); U:Refresh() end)
  button(self.body,"Остановить",415,0,110,function() C.BuyQueue:Stop() end)
  if pending then
    label(self.body,"Операция: " .. pending.stage .. "; " .. C.Items:Name(pending.item_id) .. " (#" .. pending.item_id .. ") × " .. pending.quantity .. "; цена " .. money(pending.cost),0,-34)
    if pending.stage=="unknown" then
      label(self.body,"После проверки почты/истории введите фактические количество и стоимость (copper):",0,-66)
      local qty=edit(self.body,8,-90,90,pending.quantity)
      local cost=edit(self.body,110,-90,130,pending.cost)
      button(self.body,"Подтверждаю: куплено",250,-90,185,function() C.Execution:Reconcile(true,tonumber(qty:GetText()),tonumber(cost:GetText())) end)
      button(self.body,"Не куплено",445,-90,120,function() C.Execution:Reconcile(false) end)
    end
  end
  if task then
    local package=C.db.packages[task.package_id]
    label(self.body,"Бюджет (включая NPC): " .. money(package.budget) .. "; потрачено AH: " .. money(package.spent) .. "; золото: " .. money(GetMoney()),0,-125)
    local budget=edit(self.body,8,-150,130,package.budget)
    button(self.body,"Изменить общий бюджет",150,-150,190,function()
      local value=tonumber(budget:GetText())
      if not C.db.pending and C:Integer(value) and value>=package.spent then package.budget=value; C:Emit(task,"limit_change","budget_changed",{budget=value}); U:Refresh() end
    end)
    self:Text(self:Details(task),0,-184,625,math.max(40,self.body:GetHeight()-196))
  else label(self.body,"Очередь пустая. Добавьте проверенное задание из «Возможностей».",0,-130) end
end
function U:Limits()
  local task=C.db.tasks[self.limitTask]
  if not task then self.section="Opportunities"; return end
  local live=U:Live(task)
  label(self.body,"Цены в copper. Сохранение явно меняет пределы; затем нужен новый скан.",0,0)
  local scroll=U:Widget("ScrollFrame",self.body,"UIPanelScrollFrameTemplate","limits-inputs")
  scroll:SetPoint("TOPLEFT",0,-24); scroll:SetSize(625,self.body:GetHeight()-36); scroll:SetClipsChildren(true)
  local parent=U:Widget("Frame",scroll,nil,"limits-inputs")
  parent:SetSize(625,(#task.lines+#(task.vendor_keys or {})+5)*32+50)
  if not self.limitDraft or self.limitDraft.task_id~=task.task_id then self.limitDraft={task_id=task.task_id,fields={}} end
  local draft=self.limitDraft
  local function field(name,x,y,width,value)
    if draft.fields[name]==nil then draft.fields[name]=tostring(value or "") end
    local control=edit(parent,x,y,width,draft.fields[name])
    control:SetScript("OnTextChanged",function(self,user) if user then draft.fields[name]=self:GetText() end end)
    return control
  end
  local inputs={}
  local row=0
  for _, line in ipairs(task.lines) do
    row=row+1
    local value=line.unit_limit_copper
    local quote=line.source=="npc" and C.NPC:Quote(task,line.item_id)
    if line.source=="npc" then value=quote and quote.batch_price_copper or ""
    elseif self.limitUseCurrent and live then
      for _,plan in ipairs(live.plans) do if plan.item_id==line.item_id then value=plan.max_unit_price end end
    end
    label(parent,(line.source=="npc" and "NPC партия: " or "AH предел/шт.: ") .. C.Items:Name(line.item_id) .. " × " .. line.quantity,0,-row*32,380)
    local input={line=line,edit=field("price-" .. line.item_id,390,-row*32,140,value),quote=quote}
    if line.source=="npc" then input.quantity=field("quantity-" .. line.item_id,548,-row*32,60,quote and quote.batch_quantity or 1) end
    inputs[#inputs+1]=input
  end
  local vendorInputs={}
  if task.strategy_id=="vendor_arbitrage" then
    for _,key in ipairs(task.vendor_keys or {C_AuctionHouse.MakeItemKey(task.item_id)}) do
      row=row+1
      local candidate={item_id=task.item_id,item_key=key}
      for _,lot in ipairs(task.scans[task.item_id] and task.scans[task.item_id].rows or {}) do if C.Adapter:KeyEqual(key,lot.item_key) then candidate=lot; break end end
      local value=C.NPC:Vendor(task,candidate)
      label(parent,"NPC продажа/шт.: вариант " .. (key.itemSuffix or 0) .. ", ур. " .. (key.itemLevel or 0),0,-row*32,380)
      vendorInputs[#vendorInputs+1]={key=key,edit=field("vendor-" .. C.NPC:Key(key),390,-row*32,140,value)}
    end
  end
  local package=C.db.packages[task.package_id]
  label(parent,"Общий предел затрат задания",0,-(row+1)*32,370)
  local total=field("total",390,-(row+1)*32,140,self.limitUseCurrent and live and live.cost_copper or task.max_total_cost_copper)
  label(parent,"Общий бюджет пакета (включая NPC)",0,-(row+2)*32,370)
  local budget=field("budget",390,-(row+2)*32,140,package and package.budget)
  button(parent,"Сохранить пределы и подтверждённые цены NPC",0,-(row+3)*32,405,function()
    if C.db.pending or C.Scanner.active then U.notice="Сначала завершите текущую операцию"; U:Refresh(); return end
    local totalValue,budgetValue=tonumber(total:GetText()),tonumber(budget:GetText())
    if not package or not C:Integer(totalValue) or totalValue<(task.spent or 0) or not C:Integer(budgetValue) or budgetValue<package.spent then U.notice="Неверный общий предел или бюджет"; U:Refresh(); return end
    for _,entry in ipairs(inputs) do
      if not C:Integer(tonumber(entry.edit:GetText()),entry.line.source=="npc") then U.notice="Введите текущие NPC-цены и положительные пределы AH"; U:Refresh(); return end
      if entry.quantity and not C:Integer(tonumber(entry.quantity:GetText())) then U.notice="Укажите размер NPC-партии в штуках"; U:Refresh(); return end
    end
    for _,entry in ipairs(vendorInputs) do if not C:Integer(tonumber(entry.edit:GetText()),true) then U.notice="Введите известные NPC-цены продажи вариантов"; U:Refresh(); return end end
    for _,entry in ipairs(inputs) do
      local value=tonumber(entry.edit:GetText())
      if entry.line.source=="npc" then
        local quantity=tonumber(entry.quantity:GetText())
        if not entry.quote or value~=entry.quote.batch_price_copper or quantity~=entry.quote.batch_quantity or not C.NPC:Quote(task,entry.line.item_id) then C.NPC:Manual(task,entry.line.item_id,value,quantity) end
      else entry.line.unit_limit_copper=value end
    end
    for _,entry in ipairs(vendorInputs) do C.NPC:ManualVendor(task,entry.key,tonumber(entry.edit:GetText())) end
    task.max_total_cost_copper=totalValue; package.budget=budgetValue; task.allow_overbuy=false
    task.validation=nil; task.scans={}
    C.BuyQueue.ticket=nil
    C:Emit(task,"limit_change","unit_limits_changed",{lines=task.lines,npc_prices=task.npc_prices,vendor_prices=task.vendor_prices,max_total_cost_copper=totalValue,budget=budgetValue,allow_overbuy=task.allow_overbuy})
    U.section="Opportunities"; C:State(task,"pending_scan","Пределы изменены игроком; повторите проверку")
  end)
  self:AttachScrollChild(scroll,parent)
end
function U:Render()
  if not self.frame or not C.db then return end
  local ahHeight=AuctionHouseFrame:GetHeight()
  local height=ahHeight>0 and math.min(500,ahHeight-110) or 500
  self.frame:SetHeight(height)
  self.body:SetHeight(height-105)
  self:Clear()
  self.note:SetText("Cennik " .. C.VERSION .. " | рынок " .. (C.db.current_market or "не выбран") .. " | исполнение подтверждает игрок")
  self.bindingNote:SetText(self:BindingStatus())
  local bound=C:MarketBindingMatches(C.db.current_market)
  self.bindingNote:SetTextColor(bound and 0.3 or 1,bound and 1 or 0.82,bound and 0.3 or 0)
  self.bindButton:SetText(bound and "Рынок подтверждён" or "Подтвердить рынок")
  self.noticeNote:SetText(self.notice or "")
  if self.section=="Sniper" and self.SniperPanel then self:SniperPanel()
  elseif self.section=="Hidden" then self:HiddenTasks()
  elseif self.section=="Market" and C.MarketCollector then
    local collector=C.MarketCollector
    label(self.body,"Массовый скан через Auctionator; снимки хранятся постоянно в локальном Cennik",0,0,625)
    button(self.body,"Сканировать сейчас",0,-34,180,function() collector:Start(); U:Refresh() end)
    button(self.body,C.db.collector.enabled and "Автосбор: включён" or "Автосбор: выключен",190,-34,200,function()
      C.db.collector.enabled=not C.db.collector.enabled; U:Refresh()
    end)
    button(self.body,"Записать / reload",400,-34,180,function()
      if collector.packing or collector.requested_at then
        collector:Notice("Дождитесь сообщения «Полный снимок готов». Reload сейчас прервёт получение данных")
        U:Refresh()
      else ReloadUI() end
    end)
    self:Text((collector.message or "Ожидание") .. "\n\nВ очереди: " .. #(C.db.market_scans or {}) .. "/" .. collector.maxQueue ..
      "\nСледующий запрос не ранее: " .. date("%H:%M:%S",C.db.collector.next_at or 0) ..
      "\n\nПосле /reload дождитесь импорта в вебе. Ещё один /reload загрузит подтверждение архива и освободит очередь." ..
      "\nЗаполнение очереди приостанавливает новые запросы, старые снимки не удаляются.",0,-75,625,self.body:GetHeight()-80)
  elseif self.section=="History" then
    local lines={"Запись команды не доказывает покупку. Синхронизация отчётов после /reload."}
    for index=#C.db.history,math.max(1,#C.db.history-150),-1 do
      local e=C.db.history[index]
      local task=C.db.tasks[e.task_id]
      local item=e.item_id or (task and task.item_id)
      local kind=({state="Состояние",scan="Скан",purchase="Покупка",error="Ошибка",limit_change="Пределы"})[e.kind] or e.kind
      table.insert(lines,date("%d.%m %H:%M:%S",e.observed_at) .. " | " .. kind .. " | " .. (states[e.state] or e.state) .. " | " .. (item and (C.Items:Name(item) .. " (#" .. item .. ")") or "задание") .. (e.kind=="purchase" and (" × " .. e.quantity .. " | " .. money(e.cost_copper)) or "") .. (e.details and e.details.reason and (" | " .. e.details.reason) or ""))
    end
    self:Text(table.concat(lines,"\n"),0,0,625,self.body:GetHeight()-15)
  elseif self.section=="Buy Queue" then self:Queue()
  elseif self.section=="Limits" then self:Limits()
  else self:Opportunities() end
  self.resetScroll=false
end
function U:Refresh()
  if self.rendering then self:QueueRefresh(); return end
  self.rendering=true
  local ok,reason=pcall(self.Render,self)
  self.rendering=false
  if not ok then error(reason) end
end
