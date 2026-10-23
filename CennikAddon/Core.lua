local _, C = ...
_G.Cennik = C
C.VERSION = "0.4.1"
C.MAX_INTEGER = 9007199254740991
function C:Now() return GetServerTime() end
function C:Integer(v, zero)
  return type(v) == "number" and v == math.floor(v) and v >= (zero and 0 or 1) and v <= self.MAX_INTEGER
end
function C:Refresh() if self.UI then self.UI:Refresh() end end
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
  return event
end
function C:BindMarket(market)
  if self.db.pending then return false,"Сначала разрешите неизвестный результат покупки" end
  local faction = string.lower(UnitFactionGroup("player") or "")
  if type(market) ~= "string" or not market:match("^forever%.") or not market:find("%." .. faction .. "%.") then
    return false,"Рынок должен соответствовать фракции персонажа"
  end
  self.db.binding = {market=market, realm=GetRealmName(), faction=faction, character=UnitName("player")}
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
  if task.hidden then return false,"Задание скрыто игроком" end
  if not self:MarketBindingMatches(task.market) then
    return false,"Подтвердите рынок для этого персонажа: /cennik market <market>"
  end
  if task.run_id ~= self.db.current_run or task.market ~= self.db.current_market then return false,"Устаревший run_id или другой рынок" end
  if self:Now() >= task.expires_at_epoch then return false,"Срок задания истёк: создайте задание после нового ручного расчёта" end
  if not self.Adapter:Open() then return false,"Откройте аукцион" end
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
  self.Adapter:Initialize()
  if self.MarketCollector then self.MarketCollector:Initialize() end
  if self.Sniper then self.Sniper:Initialize() end
end
local frame = CreateFrame("Frame")
C.eventFrame = frame
frame:RegisterEvent("ADDON_LOADED")
frame:RegisterEvent("AUCTION_HOUSE_SHOW")
frame:RegisterEvent("AUCTION_HOUSE_CLOSED")
frame:SetScript("OnEvent", function(_, event, ...)
  if event == "ADDON_LOADED" and ... == "CennikAddon" then C:Initialize()
  elseif C.db and event == "AUCTION_HOUSE_SHOW" then C.UI:Attach()
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
  if market then
    local ok, reason = C:BindMarket(market)
    print(ok and "Cennik: рынок подтверждён" or ("Cennik: " .. reason))
  elseif C.Adapter:Open() then
    C.UI:Attach(); C.UI:Show()
    if message=="sniper" or message=="снайпер" then C.UI:SelectSection("Sniper")
    else C.UI:Refresh() end
  else print("Cennik: откройте аукцион. Диагностика: " .. (C.Adapter.error or "Auctionator 340, современный AH")) end
end
