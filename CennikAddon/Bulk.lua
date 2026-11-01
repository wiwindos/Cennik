local _, C = ...
-- Research only: no task states, purchase tickets or protected purchase calls.
local B={token=0,lastSend=-100}
C.Bulk=B
local function keyText(key)
  return table.concat({key.itemID,key.itemLevel or 0,key.itemSuffix or 0,key.battlePetSpeciesID or 0},":")
end
function B:Initialize()
  if C.db.bulk_checks and C.db.bulk_checks.workspace_id~=C.db.workspace_id then
    C.db.bulk_profiles=C.db.bulk_profiles or {}
    local old=C.db.bulk_checks
    if old.workspace_id then C.db.bulk_profiles[old.workspace_id]=old end
    C.db.bulk_checks=C.db.bulk_profiles[C.db.workspace_id or "local"]
  end
  C.db.bulk_checks=C.db.bulk_checks or {reports={},sequence=0}
  local db=C.db.bulk_checks
  db.workspace_id=C.db.workspace_id
  db.reports=db.reports or {}
  local incoming=CennikImport and CennikImport.bulk_checks
  local ack={}
  for _,id in ipairs(incoming and incoming.acknowledged_report_ids or {}) do ack[id]=true end
  local kept={}
  for _,report in ipairs(db.reports) do if not ack[report.report_id] then kept[#kept+1]=report end end
  db.reports=kept
  if db.running then
    db.running.status,db.running.reason,db.running.completed_at="cancelled","Проверка прервана reload",C:Now()
    if #db.reports==0 then db.reports[1]=db.running end
    db.running=nil
  end
  local packet=incoming and incoming.packet
  if packet and packet.format_version==1 and packet.workspace_id==C.db.workspace_id
    and type(packet.item_ids)=="table" and #packet.item_ids>0 and #packet.item_ids<=200 then
    local valid,seen=true,{}
    for _,id in ipairs(packet.item_ids) do
      if not C:Integer(id) or id>2147483647 or seen[id] then valid=false end
      seen[id]=true
    end
    if valid and packet.max_rows==8000 and packet.max_item_rows==1000 and packet.max_variants==20 then
      db.packet=C:Clone(packet)
      if not C.db.current_market then C.db.current_market=packet.market end
      if not db.completed_batch or db.completed_batch~=packet.batch_id then
        if C.UI then C.UI.section="Bulk" end
      end
    end
  end
end
function B:Finish(status,reason)
  if not self.active then return end
  self.active,self.current=false,nil; self.token=self.token+1
  local db=C.db.bulk_checks
  self.report.status=status; self.report.reason=reason or ""; self.report.completed_at=C:Now()
  db.reports[#db.reports+1]=self.report; db.running=nil; db.completed_batch=self.packet.batch_id
  self.message=(status=="complete" and "Проверка завершена" or "Проверка неполная") .. ". /reload, затем синхронизация и выбор в вебе."
  C:Refresh()
end
function B:Cancel(reason) self:Finish("cancelled",reason or "Остановлено игроком") end
function B:Start()
  local db=C.db.bulk_checks
  local p=db and db.packet
  local function no(reason) B.message=reason; C:Refresh(); return false,reason end
  if not p then return no("Сначала отправьте пакет проверки из веба и выполните /reload") end
  if self.active then return no("Проверка уже идёт") end
  if #db.reports>0 then return no("Сначала /reload и синхронизация отчёта с вебом; затем /reload для подтверждения") end
  if p.workspace_id~=C.db.workspace_id or not C:MarketBindingMatches(p.market) then return no("Подтвердите рынок этого персонажа") end
  if C:Now()>=p.expires_at then return no("Пакет истёк; создайте новый в вебе") end
  if C.db.pending or C.Scanner.active or C.BuyQueue.ticket or C.Adapter.shopping or C.Adapter.fullScan
    or (C.MarketCollector and (C.MarketCollector.requested_at or C.MarketCollector.packing)) then return no("Сначала завершите текущую операцию AH") end
  if not C.Adapter:Open() or C.Adapter.error then return no("Откройте аукцион с совместимым Auctionator") end
  if C.Sniper then C.Sniper:HoldForManualSearch("Массовая проверка из веба") end
  self.token=self.token+1; self.active=true; self.packet=C:Clone(p); self.index=0; self.rowCount=0; self.failed=false
  db.sequence=(db.sequence or 0)+1
  self.report={format_version=1,report_id=C.db.device_id .. ":bulk:" .. C:Now() .. ":" .. db.sequence,batch_id=p.batch_id,
    workspace_id=p.workspace_id,market=p.market,started_at=C:Now(),status="partial",items={}}
  db.running=self.report
  self:NextItem()
  return true
end
function B:FailItem(reason)
  self.failed=true
  self.book.status,self.book.rows,self.book.reason="failed",{},reason
  self:StoreBook()
end
function B:StoreBook()
  self.book.observed_at=self.book.observed_at or C:Now()
  self.report.items[#self.report.items+1]=self.book
  self.rowCount=self.rowCount+#self.book.rows
  self:NextItem()
end
function B:NextItem()
  self.current=nil; self.index=self.index+1
  if self.index>#self.packet.item_ids then self:Finish(self.failed and "partial" or "complete"); return end
  if C:Now()-self.report.started_at>900 then self:Finish("partial","Проверка длится более 15 минут; разделите пакет"); return end
  local item=self.packet.item_ids[self.index]
  self.book={item_id=item,status="complete",rows={},variants_count=0}
  self.seenAuctions={}; self.keys=nil; self.keyIndex=0
  self.message="Проверка " .. self.index .. "/" .. #self.packet.item_ids .. ": #" .. item
  self.current={kind="loading",started=GetTime()}
  local token=self.token
  C.Items:Load(item,function(info,reason)
    if not B.active or B.token~=token then return end
    if not info then B:FailItem(reason or "Предмет не загрузился"); return end
    if C.Adapter:Equipment(item)==false then
      B.keys={C_AuctionHouse.MakeItemKey(item)}; B:NextKey()
    elseif C_AuctionHouse.SendBrowseQuery and C_AuctionHouse.HasFullBrowseResults and C_AuctionHouse.GetBrowseResults then
      B.current={kind="browse",name=info.name,started=GetTime(),sent=false,pages=0}; B:Poll(token)
    else B:FailItem("Клиент не поддерживает поиск точных вариантов") end
  end)
  C:Refresh()
end
function B:NextKey()
  self.keyIndex=self.keyIndex+1
  if self.keyIndex>#self.keys then self.book.variants_count=#self.keys; self:StoreBook(); return end
  self.current={kind="detail",key=self.keys[self.keyIndex],started=GetTime(),sent=false,pages=0}
  self:Poll(self.token)
end
function B:Ready()
  return C.Adapter:Ready("bulk") and GetTime()-self.lastSend>=1.3
    and (not C.Sniper or C.Sniper:Traffic()<45)
end
function B:Send(name,...)
  if not self:Ready() then return false end
  self.lastSend=GetTime()
  local ok=C.Adapter:Call(name,...)
  if not ok then self:FailItem("API отклонил " .. name) end
  return ok
end
function B:Poll(token)
  if not self.active or token~=self.token or not self.current then return end
  local p=self.current
  if not C.Adapter:Open() then self:Cancel("Аукцион закрыт"); return end
  if C:Now()-self.report.started_at>900 then self:Finish("partial","Предел проверки 15 минут; разделите пакет"); return end
  if GetTime()-p.started>30 then self:FailItem("Таймаут; наличие товара неизвестно"); return end
  if p.more and self:Ready() then
    local name=p.more; p.more=nil
    if p.pages>=10 or not C_AuctionHouse[name] then self:FailItem("Неполные страницы результатов"); return end
    p.pages=p.pages+1; self:Send(name,p.moreRef)
  elseif not p.sent and self:Ready() then
    p.sent=true
    if p.kind=="browse" then self:Send("SendBrowseQuery",{searchString=p.name,sorts={},filters={},itemClassFilters={}})
    else self:Send("SendSearchQuery",p.key,{{sortOrder=Enum.AuctionHouseSortOrder.Price,reverseSort=false}},true) end
  end
  C_Timer.After(.25,function() if B.current==p then B:Poll(token) end end)
end
function B:OnEvent(event,ref)
  local p=self.current
  if not self.active or not p or not p.sent then return end
  if event=="AUCTION_HOUSE_BROWSE_FAILURE" then self:FailItem("Ошибка поиска вариантов"); return end
  if p.kind=="browse" then
    if event~="AUCTION_HOUSE_BROWSE_RESULTS_UPDATED" and event~="AUCTION_HOUSE_BROWSE_RESULTS_ADDED" then return end
    if not C_AuctionHouse.HasFullBrowseResults() then p.more="RequestMoreBrowseResults"; return end
    local keys,seen={},{}
    for _,row in ipairs(C_AuctionHouse.GetBrowseResults() or {}) do
      local k=row.itemKey
      if k and k.itemID==self.book.item_id and not seen[keyText(k)] then
        keys[#keys+1]=C:Clone(k); seen[keyText(k)]=true
      end
    end
    if #keys>20 then self:FailItem("Более 20 вариантов; уточните предмет отдельно"); return end
    table.sort(keys,function(a,b) return keyText(a)<keyText(b) end)
    self.keys=keys; self:NextKey(); return
  end
  local commodity=event=="COMMODITY_SEARCH_RESULTS_UPDATED"
  if not commodity and event~="ITEM_SEARCH_RESULTS_UPDATED" then return end
  if commodity and ref~=self.book.item_id or not commodity and not C.Adapter:KeyEqual(ref,p.key) then return end
  local full=commodity and C_AuctionHouse.HasFullCommoditySearchResults(ref) or not commodity and C_AuctionHouse.HasFullItemSearchResults(ref)
  if not full then
    p.more=commodity and "RequestMoreCommoditySearchResults" or "RequestMoreItemSearchResults"
    p.moreRef=ref
    return
  end
  local count=commodity and C_AuctionHouse.GetNumCommoditySearchResults(ref) or C_AuctionHouse.GetNumItemSearchResults(ref)
  if count+#self.book.rows>1000 or count+#self.book.rows+self.rowCount>8000 then self:FailItem("Слишком много строк предложений; разделите пакет"); return end
  self.book.observed_at=math.min(self.book.observed_at or C:Now(),C:Now())
  for _,r in ipairs(C.Adapter:Results(self.book.item_id,p.key,commodity)) do
    if C:Integer(r.quantity) and C:Integer(r.unit_price) and (commodity or r.auction_id and not self.seenAuctions[r.auction_id]) then
      local k=r.item_key
      local row={item_key={itemID=k.itemID,itemLevel=k.itemLevel or 0,itemSuffix=k.itemSuffix or 0,battlePetSpeciesID=k.battlePetSpeciesID or 0},
        commodity=commodity,quantity=r.quantity,unit_price=r.unit_price,auction_id=not commodity and r.auction_id or nil,
        ownership_known=not not r.ownership_known,owned=not not r.owned,available_quantity=r.available_quantity,
        vendor_sell_copper=C.Adapter:VendorPrice(r)}
      self.book.rows[#self.book.rows+1]=row
      if not commodity then self.seenAuctions[r.auction_id]=true end
    end
  end
  self:NextKey()
end
local frame=CreateFrame("Frame")
for _,event in ipairs({"ITEM_SEARCH_RESULTS_UPDATED","COMMODITY_SEARCH_RESULTS_UPDATED","AUCTION_HOUSE_BROWSE_RESULTS_UPDATED","AUCTION_HOUSE_BROWSE_RESULTS_ADDED","AUCTION_HOUSE_BROWSE_FAILURE","AUCTION_HOUSE_CLOSED","AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED"}) do frame:RegisterEvent(event) end
frame:SetScript("OnEvent",function(_,event,ref)
  if not B.active then return end
  if event=="AUCTION_HOUSE_CLOSED" then B:Cancel("Аукцион закрыт")
  elseif event=="AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED" then B:Cancel("Запрос отклонён ограничением API; повторите позже")
  else B:OnEvent(event,ref) end
end)
