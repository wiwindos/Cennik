local _, C = ...
_G.Cennik = C
C.VERSION = "0.16.0"
C.MAX_INTEGER = 9007199254740991
function C:Now() return GetServerTime() end
function C:Integer(v, zero)
  return type(v) == "number" and v == math.floor(v) and v >= (zero and 0 or 1) and v <= self.MAX_INTEGER
end
function C:Refresh()
  if self.collectorOnly then if self.CollectorUI then self.CollectorUI:Refresh() end
  elseif self.UI then self.UI:Refresh() end
end
function C:Clone(value)
  if type(value)~="table" then return value end
  local copy={}
  for key,entry in pairs(value) do copy[key]=self:Clone(entry) end
  return copy
end
function C:State(task, state, reason)
  task.state, task.reason = state, reason
  self:Emit(task, "state", state, {reason=reason})
  self:Refresh()
end
function C:HideTask(task)
  if self.db.pending and self.db.pending.task_id==task.task_id then return false,"Сначала завершите или уточните результат покупки этого задания" end
  if self.Scanner.active and self.Scanner.task==task then self.Scanner:Cancel("Задание скрыто игроком") end
  self.BuyQueue.ticket=nil
  task.hidden=true
  local queue={}
  for _,id in ipairs(self.db.queue) do if id~=task.task_id then table.insert(queue,id) end end
  self.db.queue=queue
  self:State(task,"cancelled","Задание скрыто игроком; покупки и история сохранены")
  return true
end
function C:DeleteHiddenTask(task)
  if not task or not task.hidden then return false,"Сначала скройте задание" end
  if self.db.pending and self.db.pending.task_id==task.task_id then return false,"Сначала уточните результат покупки" end
  self:Emit(task,"state","cancelled",{reason="Задание удалено из скрытых; история сохранена"},task.item_id)
  self.db.deleted_tasks=self.db.deleted_tasks or {}; self.db.deleted_tasks[task.task_id]=true
  self.db.tasks[task.task_id]=nil
  return true
end
function C:Emit(task, kind, state, details, item, quantity, cost)
  self.db.sequence = (self.db.sequence or 0) + 1
  local event = {event_id=self.db.device_id .. ":" .. self.db.sequence,
    task_id=task.task_id, opportunity_id=task.opportunity_id, run_id=task.run_id, market=task.market,
    kind=kind, state=state, observed_at=self:Now(), item_id=item,
    quantity=quantity or 0, cost_copper=cost or 0, details=self:Clone(details or {})}
  table.insert(self.db.events,event)
  table.insert(self.db.history,event)
  if #self.db.history>500 then table.remove(self.db.history,1) end
  if self.Accounting then self.Accounting:TaskPurchase(event,task) end
  return event
end
function C:BindMarket(market)
  if self.db.pending then return false,"Сначала разрешите неизвестный результат покупки" end
  local faction = string.lower(UnitFactionGroup("player") or "")
  if type(market) ~= "string" or not market:match("^forever%.") or not market:find("%." .. faction .. "%.") then
    return false,"Рынок должен соответствовать фракции персонажа"
  end
  self.db.binding = {market=market, realm=GetRealmName(), faction=faction, character=UnitName("player")}
  if not self.collectorOnly and self.Accounting and self.Accounting.db then self.Accounting:Opening() end
  self:Refresh()
  return true
end
function C:MarketBindingMatches(market)
  local binding=self.db and self.db.binding
  return not not (type(market)=="string" and binding and binding.market==market
    and binding.realm==GetRealmName() and binding.faction==string.lower(UnitFactionGroup("player") or "")
    and binding.character==UnitName("player"))
end
function C:CheckTask(task, ownPending)
  if self.collectorOnly then return false,"Включён режим сборщика: стратегии и закупки недоступны" end
  if self.Bulk and self.Bulk.active then return false,"Дождитесь завершения массовой проверки" end
  if task.hidden then return false,"Задание скрыто игроком" end
  local details=task.opportunity and task.opportunity.details
  if task.strategy_id=="npc_to_auction" and task.format_version~=3 then
    return false,"Устаревший формат задания NPC → AH; создайте новый пакет"
  end
  if (task.strategy_id=="buy_disenchant" or task.strategy_id=="craft_disenchant") and task.format_version~=2 then
    return false,"Устаревший формат задания распыления; создайте новый пакет"
  end
  if details and details.workspace_plan_id and details.quantity_model~="forever_units_v1" then
    return false,"План использует прежнюю модель количества. Пересчитайте партию в вебе и передайте новое задание"
  end
  if not self:MarketBindingMatches(task.market) then
    return false,"Подтвердите рынок для этого персонажа: /cennik market <market>"
  end
  if task.run_id ~= self.db.current_run or task.market ~= self.db.current_market then return false,"Устаревший run_id или другой рынок" end
  if self:Now() >= task.expires_at_epoch then return false,"Срок задания истёк: создайте задание после нового ручного расчёта" end
  if not self.Adapter:Open() then return false,"Откройте аукцион" end
  local contextOK,contextReason=self.AuctionContext:Check("buy")
  if not contextOK then return false,contextReason end
  if self.db.pending and not (ownPending and self.db.pending.task_id == task.task_id) then return false,"Результат предыдущей операции ещё не определён" end
  return true
end
function C:Initialize()
  CennikAddonDB = CennikAddonDB or {}
  self.db = CennikAddonDB
  self.db.tasks = self.db.tasks or {}
  self.db.packages = self.db.packages or {}
  self.db.queue = self.db.queue or {}
  self.db.events = self.db.events or {}
  self.db.history = self.db.history or {}
  self.db.deleted_tasks = self.db.deleted_tasks or {}
  self.db.device_id = self.db.device_id or (CennikImport and CennikImport.device_id) or ("offline-" .. self:Now())
  self.db.sequence = self.db.sequence or 0
  if self.db.pending then
    self.db.pending.stage = "unknown"
    local task = self.db.tasks[self.db.pending.task_id]
    if task and not self.db.pending.sniper then self:State(task,"requires_manual_check","Покупка прервана загрузкой аддона; проверьте почту/историю до продолжения") end
  end
  self:Import()
  self.collectorOnly=CennikImport and CennikImport.collector_only==true
  local binding=CennikImport and CennikImport.market_binding
  if CennikImport and CennikImport.market_catalog then
    binding=nil
    local matches=0
    for _,b in ipairs(CennikImport.market_catalog) do
      if b.faction==string.lower(UnitFactionGroup("player") or "") then
        for _,realm in ipairs(b.realm_names or {}) do if realm==GetRealmName() then binding=b;matches=matches+1 end end
      end
    end
    if matches~=1 then binding=nil end
    CennikImport.market_collector.market=binding and binding.market or "unassigned"
    self.db.binding=nil;self.db.current_market=binding and binding.market or nil
  end
  self.db.server_binding=binding and C:Clone(binding) or nil
  if binding then
    local allowed=false
    for _,realm in ipairs(binding.realm_names or {}) do if realm==GetRealmName() then allowed=true end end
    if allowed and binding.faction==string.lower(UnitFactionGroup("player") or "") then
      self.db.current_market=binding.market;self:BindMarket(binding.market)
    else self.db.binding=nil end
  end
  if self.collectorOnly then
    if not self.db.collector_device_id then
      local hash=5381
      for byte in (GetRealmName() .. ":" .. UnitName("player")):gmatch(".") do hash=(hash*33+byte:byte())%4294967296 end
      self.db.collector_device_id="collector-" .. tostring(CennikImport.collector_agent_id) .. "-" .. string.format("%08x",hash)
    end
    self.db.device_id=self.db.collector_device_id
    self.db.queue={};self.db.current_run=nil
    if not (CennikImport.market_catalog or binding) and string.lower(UnitFactionGroup("player") or "")=="alliance" then
      self.db.current_market="forever.pvp.alliance.us";self:BindMarket(self.db.current_market)
    end
  end
  if self.db.quantity_model~="forever_units_v1" then
    for _,task in pairs(self.db.tasks) do task.scans={}; task.validation=nil; task.allow_overbuy=false end
    self.db.quantity_model="forever_units_v1"
  end
  self.Adapter:Initialize()
  if self.AuctionContext then self.AuctionContext:Initialize() end
  if self.CollectorStatus then self.CollectorStatus:Initialize() end
  if self.MarketCollector then self.MarketCollector:Initialize() end
  if self.Sniper and not self.collectorOnly then self.Sniper:Initialize() end
  if self.Bulk and not self.collectorOnly then self.Bulk:Initialize() end
  if self.Accounting then self.Accounting:Initialize() end
  if self.Production and not self.collectorOnly then self.Production:Initialize() end
  if self.CharacterRole then self.CharacterRole:Initialize() end
  if self.IdleGuard then self.IdleGuard:Initialize() end
end
local frame = CreateFrame("Frame")
C.eventFrame = frame
frame:RegisterEvent("ADDON_LOADED")
frame:RegisterEvent("AUCTION_HOUSE_SHOW")
frame:RegisterEvent("AUCTION_HOUSE_CLOSED")
frame:SetScript("OnEvent", function(_, event, ...)
  if event == "ADDON_LOADED" and ... == "CennikAddon" then C:Initialize()
  elseif C.db and event == "AUCTION_HOUSE_SHOW" then
    if not C.collectorOnly then C.UI:Attach() end
  elseif C.db and event == "AUCTION_HOUSE_CLOSED" then
    if C.Sniper and C.Sniper.ClosePopups then C.Sniper:ClosePopups(true) end
    C.Adapter.shoppingToken=(C.Adapter.shoppingToken or 0)+1
    C.UI.notice=nil
    C.BuyQueue:Stop()
  end
end)
SLASH_CENNIK1 = "/cennik"
SlashCmdList.CENNIK = function(message)
  message=(message or ""):match("^%s*(.-)%s*$"):lower()
  local market = message:match("^market%s+(.+)$")
  if C.collectorOnly then C.CollectorUI:Show(message=="diagnostics" or message=="диагностика");return end
  if message=="role" or message=="роль" then C.CharacterRole:Show();return end
  if message=="collector" or message=="сбор" then C.CollectorUI:Show();return end
  if message=="diagnostics" or message=="диагностика" then C.CollectorUI:Show(true);return end
  if message=="production" or message=="производство" then C.Production:Show();return end
  if market then
    local ok, reason = C:BindMarket(market)
    print(ok and "Cennik: рынок подтверждён" or ("Cennik: " .. reason))
  elseif C.Adapter:Open() then
    C.UI:Attach(); C.UI:Show()
    if message=="bulk" then C.UI:SelectSection("Bulk")
    elseif message=="sniper" or message=="снайпер" then C.UI:SelectSection("Sniper")
    else C.UI:Refresh() end
  else print("Cennik: откройте аукцион. Диагностика: " .. (C.Adapter.error or "Auctionator 340, современный AH")) end
end
