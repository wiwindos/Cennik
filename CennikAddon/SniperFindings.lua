local _, C = ...
local S=C.Sniper
-- Session-only observations: reload starts a new baseline, never a wave of "new" auctions.
S.findings={}
S.unreadFindings={}
local function fingerprint(lot)
  return lot.identity .. ":" .. lot.quantity .. ":" .. lot.cost
end
function S:FindingBook(rule)
  local id=rule.definition.id
  self.findings[id]=self.findings[id] or {keys={},seen={}}
  return self.findings[id]
end
function S:SyncUnreadFindings()
  local count,latest=0,nil
  local notices={}
  for id,notice in pairs(self.unreadFindings) do
    local rule=C.db.sniper.rules[id]
    if rule and not rule.removed then
      count=count+1
      notices[#notices+1]=notice
      if not latest or notice.sequence>latest.sequence then latest=notice end
    end
  end
  table.sort(notices,function(a,b) return a.first_sequence<b.first_sequence end)
  self.findingNotices=notices
  self.unreadCount=count
  self.findingHighlight=latest and latest.finding or nil
  self.findingNotice=latest and latest.message or nil
  self.findingNoticeRule=latest and latest.finding.rule_id or nil
end
function S:NotifyFinding(rule,lot,message,silent)
  self.findingSignal=(self.findingSignal or 0)+1
  local previous=self.unreadFindings[rule.definition.id]
  self.unreadFindings[rule.definition.id]={sequence=self.findingSignal,
    first_sequence=previous and previous.first_sequence or self.findingSignal,finding=C:Clone(lot),message=message}
  self:SyncUnreadFindings()
  self.overlayHidden=false; self.overlayRequested=true
  if not silent and PlaySound then PlaySound(8959,"Master") end
end
function S:ReadFinding(id)
  self.unreadFindings[id]=nil
  self:SyncUnreadFindings()
end
function S:ResetFindings(id)
  self.findings[id]=nil
  if self.latestFinding and self.latestFinding.rule_id==id then self.latestFinding=nil end
  self:ReadFinding(id)
  local rule=C.db.sniper.rules[id]
  if rule then rule.offer_count=0; rule.last_finding=nil; rule.last_match=nil; rule.last_result=nil end
end
function S:PrimeFindings(rule,keys)
  local book=self:FindingBook(rule)
  if book.initialized then return end
  book.initialized=true
  for _,key in ipairs(keys) do book.keys[self:Key(key)]={rows={},baseline=true} end
end
function S:Offers(rule)
  local rows={}
  for _,snapshot in pairs(self:FindingBook(rule).keys) do
    for _,lot in pairs(snapshot.rows) do rows[#rows+1]=lot end
  end
  table.sort(rows,function(a,b)
    if a.unit_price~=b.unit_price then return a.unit_price<b.unit_price end
    return a.identity<b.identity
  end)
  return rows
end
function S:RefreshPinned(rule)
  local book=self:FindingBook(rule); local rows=self:Offers(rule)
  if not book.pinned and rows[1] then book.pinned=C:Clone(rows[1]) end
  local pinned=book.pinned
  self.alerts[rule.definition.id]=nil
  for _,lot in ipairs(rows) do
    if pinned and fingerprint(lot)==fingerprint(pinned) then
      book.pinned=C:Clone(lot); self.alerts[rule.definition.id]=lot; break
    end
  end
  if book.pinned then rule.last_finding=C:Clone(book.pinned) end
  rule.last_match=self.alerts[rule.definition.id]~=nil
  rule.offer_count=#rows
end
function S:NextFinding(rule)
  local rows=self:Offers(rule); local book=self:FindingBook(rule)
  if #rows==0 then self:Notice("Подходящих предложений сейчас нет"); return end
  local index=0
  for i,lot in ipairs(rows) do
    if book.pinned and fingerprint(lot)==fingerprint(book.pinned) then index=i; break end
  end
  book.pinned=C:Clone(rows[index%#rows+1]); self:RefreshPinned(rule)
  self:RenderOverlay()
end
function S:CheapestFinding(rule)
  local rows=self:Offers(rule)
  return rows[1]
end
function S:SelectCheapestFinding(rule)
  if C.db.pending then self:Notice("Сначала завершите покупку"); return end
  local lot=rule and self:CheapestFinding(rule)
  if not lot then self:Notice("Подходящих предложений сейчас нет"); return end
  self:FindingBook(rule).pinned=C:Clone(lot); self.selected=rule.definition.id
  self:RefreshPinned(rule); self:RenderOverlay()
end
function S:ShowLatestFinding()
  local latest=self.latestFinding
  local rule=latest and C.db.sniper.rules[latest.rule_id]
  if not rule or rule.removed then self:Notice("Эта цель уже удалена"); return end
  if C.db.pending then self:Notice("Сначала завершите покупку"); return end
  for _,lot in ipairs(self:Offers(rule)) do
    if fingerprint(lot)==fingerprint(latest) then
      self:FindingBook(rule).pinned=C:Clone(lot); self.selected=rule.definition.id
      self:RefreshPinned(rule); self:ReadFinding(rule.definition.id); self:RenderOverlay(); return
    end
  end
  self:Notice("Новая находка уже недоступна; выберите другое предложение")
end
function S:MissingFindings(rule,key)
  local book=self:FindingBook(rule)
  book.initialized=true
  local lost
  for id,snapshot in pairs(book.keys) do
    if not key or id==self:Key(key) then for _,lot in pairs(snapshot.rows) do lost=lot; break end end
  end
  if key then
    local id=self:Key(key)
    book.keys[id]={rows={}}
  else
    for id in pairs(book.keys) do book.keys[id]={rows={}} end
  end
  rule.checked_at=GetTime(); rule.last_check_epoch=C:Now(); self.lastScanAt=C:Now()
  rule.refreshing=false; rule.reason="Подходящих предложений нет"
  self:RefreshPinned(rule)
  if lost then
    self:NotifyFinding(rule,lost,"Предложение стало недоступно · " .. C.Items:Name(rule.definition.item_id))
  end
end
function S:AcceptRows(rule,rows,key)
  if not rule.enabled or rule.removed or self:Remaining(rule)==0 then self.alerts[rule.definition.id]=nil; return end
  key=key or rows[1] and rows[1].item_key or C_AuctionHouse.MakeItemKey(rule.definition.item_id)
  self:PrimeFindings(rule,{key})
  local book=self:FindingBook(rule); local keyID=self:Key(key)
  local previous=book.keys[keyID]; local baseline=previous and previous.baseline==true
  local snapshot={rows={}}; local changed,newCount,supply,decreased,removed=0,0,0,0,0
  local latest
  local limit=self:Limit(rule)
  for _,row in ipairs(rows) do
    if limit and row.item_id==rule.definition.item_id and row.ownership_known and not row.owned
      and C:Integer(row.quantity) and C:Integer(row.unit_price) and row.unit_price<=limit
      and (row.commodity or C:Integer(row.auction_id)) then
      local available=row.available_quantity or row.quantity
      local quantity=row.commodity and math.min(self:Remaining(rule),available) or 1
      local cost=row.commodity and row.unit_price*quantity or row.total_price
      if C:Integer(cost) then
        local identity=keyID .. (row.commodity and (":price:" .. row.unit_price) or (":auction:" .. row.auction_id))
        local lot={identity=identity,rule_id=rule.definition.id,item_id=row.item_id,key=C:Clone(row.item_key or key),commodity=row.commodity,
          auction_id=row.auction_id,quantity=quantity,cost=cost,unit_price=row.unit_price,limit=limit,at=C:Now(),available=available}
        local seen=book.seen[identity]
        local old=previous and previous.rows[identity]
        lot.event=old and old.event or "existing"
        if not baseline then
          if not seen then
            if row.commodity then supply=supply+1; lot.event="supply" else newCount=newCount+1; lot.event="new" end
            latest=latest or lot
          elseif not old then
            if row.commodity then supply=supply+1; lot.event="supply" else newCount=newCount+1; lot.event="new" end
            latest=latest or lot
          elseif available>old.available then supply=supply+1; lot.event="supply"; latest=latest or lot
          elseif old and available<old.available then decreased=decreased+1; lot.event="supply"; latest=latest or lot
          elseif not row.commodity and seen.fingerprint~=fingerprint(lot) then changed=changed+1; lot.event="changed"; latest=latest or lot end
        end
        book.seen[identity]={fingerprint=fingerprint(lot)}
        snapshot.rows[identity]=lot
      end
    end
  end
  local lost
  if previous and not baseline then
    for identity,old in pairs(previous.rows) do
      if not snapshot.rows[identity] then removed=removed+1; lost=lost or old end
    end
  end
  book.keys[keyID]=snapshot
  rule.checked_at=GetTime(); rule.last_check_epoch=C:Now(); self.lastScanAt=C:Now()
  rule.refreshing=false; rule.search_error=nil; rule.last_result=C:Clone(C.Adapter.resultDiagnostic)
  self:RefreshPinned(rule)
  local lot=self.alerts[rule.definition.id]
  rule.reason=lot and ("Найдено предложение" .. (lot.cost>GetMoney() and "; на этом персонаже недостаточно золота для покупки" or "")) or "Подходящих предложений нет"
  if #rows==0 and rule.last_result and rule.last_result.item_id==rule.definition.item_id and rule.last_result.quantity_warning then rule.reason=rule.last_result.quantity_warning end
  if newCount+changed+supply+decreased+removed>0 then
    local label=newCount>0 and ("Новых лотов: " .. newCount) or supply>0 and "Изменилось предложение товара"
      or changed>0 and "Изменилась цена лота" or decreased>0 and "Изменилось доступное количество" or "Предложение стало недоступно"
    if latest then self.latestFinding=C:Clone(latest) end
    local message=label .. " · " .. C.Items:Name(rule.definition.item_id)
    if latest then message=message .. "\n" .. latest.quantity .. " шт. · всего " .. GetMoneyString(latest.cost,true) end
    self:NotifyFinding(rule,latest or lost,message)
  elseif baseline and lot then
    self:NotifyFinding(rule,lot,"Уже есть на аукционе · " .. C.Items:Name(rule.definition.item_id),true)
  end
  if not self.selected and lot then self.selected=rule.definition.id end
  self:RenderOverlay()
end
