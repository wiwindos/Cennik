local _, C = ...
-- Cennik-owned implementation. PBS KeyCache.lua was audited as a design reference:
-- SearchForItemKeys batches discovery; executable lots always get a detailed query.
local S={elapsed=0,interval=1.25,minimumInterval=1.25,window={},alerts={},details={},token=0}
C.Sniper=S
local weights={high=3,normal=2,low=1}
local function split(text,separator)
  local result={}
  for part in (text .. separator):gmatch("(.-)" .. separator) do result[#result+1]=part end
  return result
end
function S:Key(key)
  return key.itemID .. ":" .. (key.itemLevel or 0) .. ":" .. (key.itemSuffix or 0) .. ":" .. (key.battlePetSpeciesID or 0)
end
function S:Remaining(rule) return math.max(0,rule.goal-(rule.bought or 0)) end
function S:Notice(text)
  self.message=text; self.message_at=C:Now()
  if self.RenderOverlay then self:RenderOverlay() end
end
function S:Emit(kind,fields)
  local db=C.db.sniper
  db.sequence=(db.sequence or 0)+1
  local event={id=C.db.device_id .. ":snipe:" .. db.sequence,kind=kind,at=C:Now(),base_revision=db.revision}
  for key,value in pairs(fields or {}) do event[key]=C:Clone(value) end
  db.events[#db.events+1]=event
  db.history=db.history or {}; db.history[#db.history+1]=C:Clone(event)
  if #db.history>200 then table.remove(db.history,1) end
end
function S:Validate(def)
  if type(def)~="table" or type(def.id)~="string" or #def.id>80 or not def.id:match("^[%w_-]+$") then return nil,"Неверный ID цели" end
  if def.market~="forever.pvp.alliance.us" or not C:Integer(def.item_id) or def.item_id>2147483647 then return nil,"Другой рынок или неверный ID предмета" end
  if not C:Integer(def.quantity) or def.quantity>1000000 or not weights[def.priority] then return nil,"Неверное количество или приоритет" end
  if def.mode~="any" and def.mode~="limit" and def.mode~="median3d" then return nil,"Неизвестный режим цены" end
  if not C:Integer(def.unit_limit_copper,true) or not C:Integer(def.discount_bps,true) or def.discount_bps>10000 then return nil,"Неверная цена или скидка" end
  if def.mode=="limit" and def.unit_limit_copper==0 then return nil,"Укажите положительную цену за штуку" end
  if def.use_bags~=true and def.use_bags~=false then return nil,"Неверный режим запаса" end
  if def.item_level~=nil or def.item_suffix~=nil or def.pet_id~=nil then
    if not C:Integer(def.item_level,true) or def.item_level>100000 or not C:Integer(def.pet_id,true) or def.pet_id>2147483647 or type(def.item_suffix)~="number" or def.item_suffix~=math.floor(def.item_suffix) or math.abs(def.item_suffix)>2147483647 then return nil,"Нужен полный ключ варианта" end
  end
  return C:Clone(def)
end
function S:Add(def,quiet)
  local valid,reason=self:Validate(def)
  if not valid then return false,reason end
  local db=C.db.sniper
  db.history=db.history or {}
  if not quiet and #db.events>=1900 then return false,"Сначала сохраните и синхронизируйте журнал" end
  local rule=db.rules[valid.id]
  if not rule then
    local count=0; for _ in pairs(db.rules) do count=count+1 end
    if count>=500 then return false,"Максимум 500 целей" end
    local get=C_Item and C_Item.GetItemCount or GetItemCount
    if valid.use_bags and type(get)~="function" then return false,"Клиент не поддерживает чтение сумок" end
    if valid.use_bags then
      for _,other in pairs(db.rules) do
        if not other.removed and other.definition.item_id==valid.item_id and other.stock>0 and self:Remaining(other)>0 then
          return false,"Запас сумок уже учтён в другой незавершённой цели; увеличьте её количество"
        end
      end
    end
    local owned=valid.use_bags and type(get)=="function" and get(valid.item_id,false,false) or 0
    if valid.use_bags and valid.item_level~=nil then return false,"Для точного варианта создайте цель без вычитания сумок" end
    if not C:Integer(owned,true) then return false,"Клиент не сообщил запас в сумках" end
    rule={definition=valid,stock=math.min(owned,valid.quantity),bought=0,spent=0,enabled=false}
    db.rules[valid.id]=rule
  else
    if C.db.pending and C.db.pending.sniper and C.db.pending.rule_id==valid.id then return false,"Сначала уточните результат покупки этой цели" end
    for _,key in ipairs({"item_id","market","item_level","item_suffix","pet_id","use_bags"}) do
      if rule.definition[key]~=valid[key] then return false,"Предмет, вариант и режим сумок существующей цели менять нельзя; создайте новую" end
    end
    rule.definition=valid; rule.enabled=false
  end
  rule.goal=math.max(0,valid.quantity-rule.stock)
  rule.keys=valid.item_level~=nil and {{itemID=valid.item_id,itemLevel=valid.item_level,itemSuffix=valid.item_suffix,battlePetSpeciesID=valid.pet_id}} or nil
  self.alerts[valid.id]=nil
  if self.ResetFindings and not self.staging then self:ResetFindings(valid.id) end
  if not quiet then self:Emit("rule_update",{rule=valid}) end
  return true,rule
end
function S:Initialize()
  local book=CennikImport and CennikImport.sniper_book
  local workspace=C.db.workspace_id or (book and book.workspace_id) or "local"
  if C.db.sniper and C.db.sniper.workspace_id~=workspace then
    if C.db.pending then return end
    C.db.sniper_profiles=C.db.sniper_profiles or {}
    C.db.sniper_profiles[C.db.sniper.workspace_id]=C.db.sniper
    C.db.sniper=C.db.sniper_profiles[workspace]
  end
  C.db.sniper=C.db.sniper or {workspace_id=workspace,rules={},events={},sequence=0,budget_copper=300000,spent=0}
  local db=C.db.sniper
  local ack={}; for _,id in ipairs((CennikImport and CennikImport.acknowledged_sniper_event_ids) or {}) do ack[id]=true end
  local events={}; for _,event in ipairs(db.events) do if not ack[event.id] then events[#events+1]=event end end; db.events=events
  if book and not C.db.pending and book.workspace_id==workspace and book.revision~=db.revision and C:Integer(book.budget_copper) then
    local incoming={}
    for _,def in ipairs(book.rules or {}) do
      incoming[def.id]=true
      local old=db.rules[def.id]
      if not old or self:DefinitionText(old.definition)~=self:DefinitionText(def) then
        local ok,reason=self:Add(def,true); if not ok then self:Notice(reason) end
      end
      if db.rules[def.id] then db.rules[def.id].reference=(book.references or {})[def.id] end
    end
    for id,rule in pairs(db.rules) do if not incoming[id] then rule.enabled=false; rule.removed=true else rule.removed=nil end end
    db.budget_copper=book.budget_copper; db.revision=book.revision
  end
  for _,rule in pairs(db.rules) do rule.refreshing=nil end
  if C.db.pending and C.db.pending.sniper then C.db.pending.stage="unknown"; self:Notice("Покупка прервана reload: проверьте почту и уточните результат") end
  if self.InstallAuctionatorButtons then self:InstallAuctionatorButtons() end
  if not self.hooked then
    self.hooked=true
    for _,name in ipairs({"SendSearchQuery","SendSellSearchQuery","SendBrowseQuery","SearchForItemKeys","RequestMoreBrowseResults","RequestMoreItemSearchResults","RequestMoreCommoditySearchResults","ReplicateItems","PlaceBid","StartCommoditiesPurchase","ConfirmCommoditiesPurchase","PostItem","PostCommodity","CancelAuction"}) do
      if C_AuctionHouse[name] then hooksecurefunc(C_AuctionHouse,name,function()
        S.window[#S.window+1]=GetTime(); S.lastSend=GetTime()
        if not C.Adapter.sending then S:HoldForManualSearch("Ручной поиск или другая операция аукциона") end
      end) end
    end
  end
end
function S:DefinitionText(def)
  local fields={def.id,def.market,def.item_id,def.quantity,def.mode,def.unit_limit_copper,def.discount_bps,def.priority,def.use_bags and 1 or 0,def.item_level or "",def.item_suffix or "",def.pet_id or ""}
  for index,value in ipairs(fields) do if type(value)=="number" then fields[index]=string.format("%.0f",value) end end
  return table.concat(fields,"\t")
end
function S:Export()
  local lines={"CENNIK-SNIPE-1\t" .. string.format("%.0f",C.db.sniper.budget_copper)}
  for _,rule in ipairs(self:Rules()) do lines[#lines+1]=self:DefinitionText(rule.definition) end
  return table.concat(lines,"\n")
end
function S:Preview(text)
  if type(text)~="string" or #text>200000 then return nil,"Список превышает 200 КБ" end
  local lines=split(text:gsub("\r",""):gsub("\n+$",""),"\n")
  local header=split(lines[1] or "","\t"); local budget=tonumber(header[2])
  if header[1]~="CENNIK-SNIPE-1" or #header~=2 or not C:Integer(budget) then return nil,"Нужен список CENNIK-SNIPE-1 с бюджетом" end
  if #lines>501 then return nil,"Максимум 500 целей" end
  local result={budget=budget,rules={}}; local ids={}
  for index=2,#lines do
    local f=split(lines[index],"\t")
    if #f~=12 or (f[9]~="0" and f[9]~="1") then return nil,"Неверная строка " .. index end
    local def={id=f[1],market=f[2],item_id=tonumber(f[3]),quantity=tonumber(f[4]),mode=f[5],unit_limit_copper=tonumber(f[6]),discount_bps=tonumber(f[7]),priority=f[8],use_bags=f[9]=="1",item_level=tonumber(f[10]),item_suffix=tonumber(f[11]),pet_id=tonumber(f[12])}
    local valid,reason=self:Validate(def)
    if not valid or ids[def.id] then return nil,reason or "Повтор ID цели" end
    ids[def.id]=true; result.rules[#result.rules+1]=valid
  end
  return result
end
function S:Apply(preview)
  if C.db.pending then return false,"Сначала уточните результат текущей покупки" end
  local count=0; for _ in pairs(C.db.sniper.rules) do count=count+1 end
  for _,def in ipairs(preview.rules) do if not C.db.sniper.rules[def.id] then count=count+1 end end
  if count>500 or #C.db.sniper.events+#preview.rules+1>1900 then return false,"Слишком много целей или событий; синхронизируйте журнал" end
  -- Stage the whole merge first: one invalid rule must never leave a partial import.
  local originals=C.db.sniper.rules; local alerts=self.alerts
  C.db.sniper.rules=C:Clone(originals); self.alerts=C:Clone(alerts)
  self.staging=true
  for _,def in ipairs(preview.rules) do
    local ok,reason=self:Add(def,true)
    if not ok then self.staging=nil; C.db.sniper.rules=originals; self.alerts=alerts; return false,reason end
  end
  self.staging=nil
  local staged=C.db.sniper.rules; C.db.sniper.rules=originals; self.alerts=alerts
  for _,def in ipairs(preview.rules) do
    local rule=originals[def.id]
    if rule then for key in pairs(rule) do rule[key]=nil end; for key,value in pairs(staged[def.id]) do rule[key]=value end
    else originals[def.id]=staged[def.id] end
    originals[def.id].removed=nil; self.alerts[def.id]=nil
    self:ResetFindings(def.id)
    self:Emit("rule_update",{rule=def})
  end
  C.db.sniper.budget_copper=preview.budget
  self:Emit("budget",{budget_copper=preview.budget})
  self:Notice("Импорт завершён; новые и изменённые цели приостановлены")
  return true
end
function S:Rules()
  local rows={}; for _,rule in pairs(C.db.sniper.rules) do if not rule.removed then rows[#rows+1]=rule end end
  table.sort(rows,function(a,b) local wa,wb=weights[a.definition.priority],weights[b.definition.priority]; if wa==wb then return a.definition.id<b.definition.id end; return wa>wb end)
  return rows
end
function S:Limit(rule)
  local def=rule.definition
  if def.mode=="any" then return C.MAX_INTEGER end
  if def.mode=="limit" then return def.unit_limit_copper end
  local ref=rule.reference
  if not ref or not C:Integer(ref.copper) or type(ref.valid_until)~="number" or C:Now()>=ref.valid_until then return nil,"Нужна история трёх завершённых дней из веба" end
  local fraction=10000-def.discount_bps
  return math.floor(ref.copper/10000)*fraction+math.floor((ref.copper%10000)*fraction/10000)
end
function S:CancelSearch(reason)
  local p=self.current
  if p and p.rule then p.rule.refreshing=false; p.rule.search_error=reason end
  for _,entry in ipairs(self.details) do entry.rule.refreshing=false end
  if self.current and self.current.kind=="lookup" and self.editor and self.editor:IsShown() then self.editor.itemInfo:SetText(reason) end
  self.token=self.token+1; self.current=nil; self.details={}; self.pauseUntil=GetTime()+1
  self.searchStatus=reason
end
function S:Pause()
  for _,rule in pairs(C.db.sniper.rules) do rule.enabled=false end
  self:CancelSearch("Снайпер приостановлен")
  self:Notice("Снайпер приостановлен")
end
function S:HoldForManualSearch(reason)
  -- A timeout cannot tell us that the player has finished reading search results.
  if not self.manualHold then self:CancelSearch(reason) end
  self.manualHold=reason
end
function S:ResumeSearch()
  if C.db.pending then self:Notice("Сначала завершите или уточните покупку"); return false end
  if not C.Adapter:Open() then self:Notice("Откройте аукцион"); return false end
  if not C:MarketBindingMatches(C.db.current_market) then self:Notice("Подтвердите рынок"); return false end
  -- Explicitly leave the manual results before allowing background queries again.
  C.UI:Attach(); C.UI:Show(); C.UI:SelectSection("Sniper")
  if not C.UI.frame or not C.UI.frame:IsShown() then return false end
  self.manualHold=nil; self.foreignTab=false
  self:Notice("Поиск продолжен во вкладке Cennik")
  return true
end
function S:Traffic()
  local recent={}; for _,at in ipairs(self.window) do if GetTime()-at<60 then recent[#recent+1]=at end end
  self.window=recent; return #recent
end
function S:CanSend()
  return not self.manualHold and not self.foreignTab and C.Adapter:Ready("sniper") and not C.db.pending and not C.Scanner.active
    and not (C.MarketCollector and (C.MarketCollector.requested_at or C.MarketCollector.packing))
    and GetTime()>=(self.pauseUntil or 0) and GetTime()-(self.lastSend or -100)>=self.interval
    and self:Traffic()<48
end
function S:Send(name,...)
  if not self:CanSend() then return false end
  self.lastSend=GetTime()
  local ok=C.Adapter:Call(name,...)
  if not ok then self:CancelSearch("API отклонил запрос " .. name) end
  return ok
end
function S:Choose()
  local best,score
  for _,rule in ipairs(self:Rules()) do
    if rule.enabled and self:Remaining(rule)>0 and GetTime()>=(rule.snooze or 0) then
      local limit,reason=self:Limit(rule); rule.reason=reason
      if limit then
        local value=(GetTime()-(rule.checked_at or -100))*weights[rule.definition.priority]
        if not best or value>score then best,score=rule,value end
      end
    end
  end
  return best
end
function S:BeginDetail(rule,key)
  rule.refreshing=true; rule.search_error=nil
  self.current={kind="detail",rule=rule,key=key,started=GetTime(),sent=false,pages=0}
end
function S:Begin(rule)
  if rule.definition.item_level==nil and rule.keys_at and GetTime()-rule.keys_at>60 then rule.keys=nil end
  if not rule.keys then
    if C.Adapter:Equipment(rule.definition.item_id)==false then rule.keys={C_AuctionHouse.MakeItemKey(rule.definition.item_id)}
    else
      local info=C.Items:Get(rule.definition.item_id)
      if not info then rule.checked_at=GetTime(); rule.reason="Загружается название/тип предмета"; return end
      if not C_AuctionHouse.SendBrowseQuery then rule.reason="Для экипировки выберите точный вариант в поиске Auctionator"; rule.checked_at=GetTime(); return end
      self.current={kind="discover",rule=rule,name=info.name,started=GetTime(),sent=false,pages=0}; return
    end
  end
  self:PrimeFindings(rule,rule.keys)
  if not self.batchDisabled and C_AuctionHouse.SearchForItemKeys and C_AuctionHouse.GetBrowseResults then
    local keys,mapping,seen={},{},{}
    local candidates=self:Rules(); local ordered={rule}
    for _,candidate in ipairs(candidates) do if candidate~=rule then ordered[#ordered+1]=candidate end end
    for _,candidate in ipairs(ordered) do
      if candidate.enabled and self:Remaining(candidate)>0 and candidate.keys and self:Limit(candidate) and GetTime()>=(candidate.snooze or 0) and GetTime()-(candidate.checked_at or -100)>=5/weights[candidate.definition.priority] then
        self:PrimeFindings(candidate,candidate.keys)
        for _,key in ipairs(candidate.keys) do
          local identity=self:Key(key)
          if not seen[identity] and #keys<100 then keys[#keys+1]=key; seen[identity]=true end
          if seen[identity] then mapping[identity]=mapping[identity] or {}; mapping[identity][#mapping[identity]+1]=candidate end
        end
      end
    end
    if #keys>1 then self.current={kind="batch",keys=keys,mapping=mapping,started=GetTime(),sent=false,pages=0}; return end
  end
  rule.key_index=((rule.key_index or 0)%#rule.keys)+1
  self:BeginDetail(rule,rule.keys[rule.key_index])
end
function S:OnSearchEvent(event,ref)
  local p=self.current
  if not p or not p.sent then return end
  if event=="AUCTION_HOUSE_BROWSE_FAILURE" then
    if p.kind=="batch" then self.batchDisabled=true end
    self:CancelSearch("Не завершён поиск; пакетный метод отключён при ошибке"); return
  end
  if p.kind=="discover" or p.kind=="batch" or p.kind=="lookup" then
    if event~="AUCTION_HOUSE_BROWSE_RESULTS_UPDATED" and event~="AUCTION_HOUSE_BROWSE_RESULTS_ADDED" then return end
    if C_AuctionHouse.HasFullBrowseResults and not C_AuctionHouse.HasFullBrowseResults() then p.more="RequestMoreBrowseResults"; return end
    local results=C_AuctionHouse.GetBrowseResults() or {}
    self.current=nil
    if p.kind=="lookup" then
      local rows,seen={},{}
      for _,row in ipairs(results) do
        local item=row.itemKey and row.itemKey.itemID
        if C:Integer(item) and not seen[item] and #rows<100 then rows[#rows+1]={item_id=item}; seen[item]=true end
      end
      p.callback(rows); self:Notice(#rows>0 and "Выберите предмет из результатов" or "Предмет не найден на аукционе; попробуйте Auctionator или ID")
    elseif p.kind=="discover" then
      local keys,seen={},{}
      for _,row in ipairs(results) do local key=row.itemKey
        if key and key.itemID==p.rule.definition.item_id and not seen[self:Key(key)] then keys[#keys+1]=C:Clone(key); seen[self:Key(key)]=true end
      end
      p.rule.checked_at=GetTime(); p.rule.last_check_epoch=C:Now()
      if #keys>0 then p.rule.keys=keys; p.rule.keys_at=GetTime(); self:PrimeFindings(p.rule,keys); self:BeginDetail(p.rule,keys[1])
      else self:MissingFindings(p.rule) end
    else
      local seen={}
      local matching={}
      for _,row in ipairs(results) do
        if row.itemKey and row.minPrice and row.minPrice>0 then
          for _,rule in ipairs(p.mapping[self:Key(row.itemKey)] or {}) do
            local identity=self:Key(row.itemKey) .. ":" .. rule.definition.id
            if not seen[identity] and row.minPrice<=(self:Limit(rule) or 0) then self.details[#self.details+1]={rule=rule,key=C:Clone(row.itemKey)}; seen[identity]=true; matching[identity]=true; rule.refreshing=true end
          end
        end
      end
      for _,key in ipairs(p.keys) do for _,rule in ipairs(p.mapping[self:Key(key)] or {}) do
        rule.checked_at=GetTime(); rule.last_check_epoch=C:Now()
        if not matching[self:Key(key) .. ":" .. rule.definition.id] then self:MissingFindings(rule,key) end
      end end
      self.searchStatus="Пакет ключей проверен: " .. #p.keys
    end
    return
  end
  local commodity=event=="COMMODITY_SEARCH_RESULTS_UPDATED"
  if not commodity and event~="ITEM_SEARCH_RESULTS_UPDATED" then return end
  if commodity and ref~=p.rule.definition.item_id or not commodity and not C.Adapter:KeyEqual(ref,p.key) then return end
  local full=commodity and C_AuctionHouse.HasFullCommoditySearchResults(ref) or not commodity and C_AuctionHouse.HasFullItemSearchResults(ref)
  if not full then p.commodity=commodity; p.more=commodity and "RequestMoreCommoditySearchResults" or "RequestMoreItemSearchResults"; return end
  self.current=nil
  self.clean=(self.clean or 0)+1
  if self.clean>=20 then self.interval=math.max(self.minimumInterval,self.interval*0.9); self.clean=0 end
  self:AcceptRows(p.rule,C.Adapter:Results(p.rule.definition.item_id,p.key,commodity),p.key)
end
function S:Tick(elapsed)
  self.elapsed=self.elapsed+elapsed; if self.elapsed<0.1 then return end; self.elapsed=0
  if not C.db or not C.db.sniper then return end
  if self.RenderOverlay then self:RenderOverlay() end
  if C.db.pending then return end
  if not C.Adapter:Open() then if self.current then self:CancelSearch("Аукцион закрыт") end; return end
  local p=self.current
  if p and GetTime()-p.started>30 then
    if p.kind=="batch" then self.batchDisabled=true end
    p.rule=p.rule or {}; p.rule.checked_at=GetTime(); self:CancelSearch("Таймаут поиска; результат неизвестен"); return
  end
  if not self:CanSend() then return end
  if not p then
    local entry=table.remove(self.details,1)
    if entry and entry.rule.enabled and self:Remaining(entry.rule)>0 then self:BeginDetail(entry.rule,entry.key)
    else local rule=self:Choose(); if not rule then return end; self:Begin(rule) end
    p=self.current; if not p then return end
  end
  local market=p.rule and p.rule.definition.market or "forever.pvp.alliance.us"
  if p.kind~="lookup" and not C:MarketBindingMatches(market) then self:Notice("Подтвердите рынок персонажа"); self.current=nil; return end
  if p.more then
    if p.pages>=10 or not C_AuctionHouse[p.more] then self:CancelSearch("Неполные результаты; покупка не готова"); return end
    p.pages=p.pages+1; local more=p.more; p.more=nil
    self:Send(more,p.commodity and p.rule.definition.item_id or p.key)
  elseif not p.sent then
    p.sent=true
    if p.kind=="batch" then self:Send("SearchForItemKeys",p.keys,{})
    elseif p.kind=="discover" or p.kind=="lookup" then self:Send("SendBrowseQuery",{searchString=p.name,sorts={},filters={},itemClassFilters={}})
    else self:Send("SendSearchQuery",p.key,{{sortOrder=Enum.AuctionHouseSortOrder.Price,reverseSort=false}},true) end
  end
end
local frame=CreateFrame("Frame")
for _,event in ipairs({"ITEM_SEARCH_RESULTS_UPDATED","COMMODITY_SEARCH_RESULTS_UPDATED","AUCTION_HOUSE_BROWSE_RESULTS_UPDATED","AUCTION_HOUSE_BROWSE_RESULTS_ADDED","AUCTION_HOUSE_BROWSE_FAILURE","AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED","AUCTION_HOUSE_CLOSED"}) do pcall(frame.RegisterEvent,frame,event) end
frame:SetScript("OnUpdate",function(_,elapsed) S:Tick(elapsed) end)
frame:SetScript("OnEvent",function(_,event,...)
  if not C.db or not C.db.sniper then return end
  if event=="AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED" then S.interval=math.min(15,S.interval*2); S.clean=0; S:CancelSearch("Ограничение API; темп снижен")
  elseif event=="AUCTION_HOUSE_CLOSED" then S:CancelSearch("Аукцион закрыт"); S.alerts={}
  else S:OnSearchEvent(event,...) end
end)
