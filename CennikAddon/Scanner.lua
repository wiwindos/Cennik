local _, C = ...
local S = {token=0}
C.Scanner = S
function S:Cancel(reason)
  self.token = self.token+1
  if self.active then C:State(self.task,"scan_failed",reason or "Скан остановлен") end
  self.active,self.current = false,nil
end
function S:Start(task,callback)
  if self.active then return false end
  task.validation,task.scans,task.needs_variant_item_id=nil,{},nil
  task.vendor_keys=nil
  C.BuyQueue.ticket=nil
  local ok,reason = C:CheckTask(task)
  if not ok then C:State(task,"requires_manual_check",reason); return false end
  if self.active or not C.Adapter:Ready() then C:State(task,"scan_failed","AH занят или ограничивает запросы"); return false end
  self.task,self.callback,self.ids,self.index,self.token = task,callback,{},0,self.token+1
  local seen = {}
  local function add(id) if not seen[id] then seen[id]=true; table.insert(S.ids,id) end end
  add(task.item_id)
  for _, line in ipairs(task.lines) do if line.source == "auction" then add(line.item_id) end end
  self.active = true
  C:State(task,"scanning")
  self:Next()
  return true
end
function S:Rejected(state,reason)
  self.token=self.token+1
  self.active,self.current=false,nil
  local validation={state=state,reason=reason}
  self.task.validation=validation
  C:State(self.task,state,reason)
  if self.callback then self.callback(self.task,validation) end
end
function S:Next()
  self.token = self.token+1
  self.current=nil
  self.index = self.index+1
  if self.index > #self.ids then
    self.active,self.current = false,nil
    C:Emit(self.task,"scan","price_checked",{scans=self.task.scans})
    local validation = C.Validator:Validate(self.task)
    self.task.validation = validation
    C:State(self.task,validation.state,validation.reason)
    local task=self.task
    C_Timer.After(91,function()
      if task.validation==validation then
        task.validation=nil; C.BuyQueue.ticket=nil
        if C.db.pending and C.db.pending.task_id==task.task_id then C:Refresh()
        else C:State(task,"pending_scan","Скан старше 90 секунд. Повторите проверку перед решением о покупке.") end
      end
    end)
    if self.callback then self.callback(self.task,validation) end
    return
  end
  local item = self.ids[self.index]
  self.task.reason="Проверка " .. self.index .. "/" .. #self.ids .. ": " .. C.Items:Name(item) .. " (#" .. item .. ")"
  C:Refresh()
  local token=self.token
  if C.Adapter:Equipment(item)==nil then
    C.Items:Load(item,function(info,reason)
      if token~=S.token or not S.active then return end
      if not info then S:Cancel(reason); return end
      S:Select(item,token)
    end)
    return
  end
  self:Select(item,token)
end
function S:Select(item,token)
  local equipment = C.Adapter:Equipment(item)
  if equipment==nil then self:Cancel("Клиент не определил тип предмета #" .. item); return end
  local canBrowse=C_AuctionHouse.SendBrowseQuery and C_AuctionHouse.GetBrowseResults and C_AuctionHouse.HasFullBrowseResults
  if equipment and ((self.task.strategy_id=="vendor_arbitrage" and canBrowse) or not self.task.selected_keys[item]) then
    self.task.needs_variant_item_id=item
    if C_AuctionHouse.SendBrowseQuery and C_AuctionHouse.GetBrowseResults and C_AuctionHouse.HasFullBrowseResults then
      C.Items:Load(item,function(info,reason)
        if token~=S.token or not S.active then return end
        if not info then S:Cancel(reason); return end
        S.current={item=item,browse=true,name=info.name,sent=false,pages=0}
        S.deadline=GetTime()+30
        S:Poll(token)
      end)
    else
      self:Rejected("requires_manual_check","Откройте " .. C.Items:Name(item) .. " (#" .. item .. ") в Shopping и подтвердите точный вариант")
    end
    return
  end
  self.task.needs_variant_item_id=nil
  self.current = {item=item,key=self.task.selected_keys[item] or C_AuctionHouse.MakeItemKey(item),sent=false,pages=0}
  if self.task.strategy_id=="vendor_arbitrage" then self.task.vendor_keys={C:Clone(self.current.key)} end
  self.deadline = GetTime()+30
  self:Poll(token)
end
function S:NextVendorVariant(item)
  self.token=self.token+1
  self.variantIndex=self.variantIndex+1
  if self.variantIndex>#self.variantKeys then
    local rows,oldest={},C:Now()
    for _,scan in ipairs(self.variantScans) do
      oldest=math.min(oldest,scan.observed_at)
      for _,row in ipairs(scan.rows) do rows[#rows+1]=row end
    end
    self.task.scans[item]={variants=self.variantScans,rows=rows,complete=true,observed_at=oldest,commodity=false}
    self.variantKeys,self.variantScans=nil,nil
    self.task.needs_variant_item_id=nil
    self:Next()
    return
  end
  self.task.reason="Проверка вариантов NPC " .. self.variantIndex .. "/" .. #self.variantKeys .. ": " .. C.Items:Name(item)
  C:Refresh()
  self.current={item=item,key=self.variantKeys[self.variantIndex],vendorVariant=true,sent=false,pages=0}
  self.deadline=GetTime()+30
  self:Poll(self.token)
end
function S:StoreRows(rows,commodity)
  local scan={rows=rows,complete=true,observed_at=self.current.received_at or C:Now(),key=self.current.key,commodity=commodity}
  if self.current.vendorVariant then
    self.variantScans[#self.variantScans+1]=scan
    self:NextVendorVariant(self.current.item)
  else self.task.scans[self.current.item]=scan; self:Next() end
end
function S:VendorRows(rows,commodity,token)
  if token~=self.token or not self.active then return end
  local unknown=false
  for _,row in ipairs(rows) do
    if row.ownership_known and not row.owned and row.quantity>0 then
      local sell,source=C.NPC:Vendor(self.task,row)
      if sell==nil then unknown=true else row.vendor_known=true; row.vendor_sell_copper=sell; row.vendor_source=source end
    end
  end
  if unknown then
    if GetTime()>self.deadline then self:Cancel("Нет NPC-цены в клиенте и базе. Введите цену продажи для варианта в «Цены / лимиты», затем повторите скан. Прибыль неизвестна."); return end
    C_Timer.After(0.25,function() S:VendorRows(rows,commodity,token) end)
  else self:StoreRows(rows,commodity) end
end
function S:Poll(token)
  if token ~= self.token or not self.active then return end
  if GetTime()>self.deadline then self:Cancel("Таймаут скана; ответ неизвестен"); return end
  if self.current.browse then
    if not self.current.sent and C.Adapter:Ready() then
      self.current.sent=true
      local ok=C.Adapter:Call("SendBrowseQuery",{searchString=self.current.name,sorts={},filters={},itemClassFilters={}})
      if not ok then self:Cancel("Ошибка поиска вариантов"); return end
    end
  elseif not self.current.sent and C.Adapter:Ready() and C_AuctionHouse.GetItemKeyInfo(self.current.key) then
    self.current.sent = true
    local sort = Enum and Enum.AuctionHouseSortOrder and Enum.AuctionHouseSortOrder.Price
    local ok = C.Adapter:Call("SendSearchQuery",self.current.key,sort and {{sortOrder=sort,reverseSort=false}} or {},true)
    if not ok then self:Cancel("Ошибка SendSearchQuery"); return end
  end
  C_Timer.After(0.25,function() S:Poll(token) end)
end
function S:OnEvent(event,ref)
  if event == "ITEM_SEARCH_RESULTS_UPDATED" and type(ref) == "table" then C.lastSelectedKey = ref end
  if not self.active or not self.current or not self.current.sent then return end
  if self.current.browse then
    if event=="AUCTION_HOUSE_BROWSE_FAILURE" then self:Cancel("Ошибка поиска вариантов AH"); return end
    if event~="AUCTION_HOUSE_BROWSE_RESULTS_UPDATED" and event~="AUCTION_HOUSE_BROWSE_RESULTS_ADDED" then return end
    if not C_AuctionHouse.HasFullBrowseResults() then
      if C_AuctionHouse.RequestMoreBrowseResults and C.Adapter:Ready() and self.current.pages<10 then
        self.current.pages=self.current.pages+1
        C.Adapter:Call("RequestMoreBrowseResults")
      else self:Cancel("Поиск вариантов неполный; повторите проверку") end
      return
    end
    local keys={}
    for _,row in ipairs(C_AuctionHouse.GetBrowseResults() or {}) do
      local key=row.itemKey
      if key and key.itemID==self.current.item then
        local duplicate=false
        for _,old in ipairs(keys) do if C.Adapter:KeyEqual(old,key) then duplicate=true end end
        if not duplicate then table.insert(keys,key) end
      end
    end
    local item=self.current.item
    if #keys==0 then
      self.task.needs_variant_item_id=nil
      self.task.scans[item]={rows={},complete=true,observed_at=C:Now(),key=C_AuctionHouse.MakeItemKey(item),commodity=false}
      self:Next()
    elseif self.task.strategy_id=="vendor_arbitrage" then
      if #keys>40 then self:Rejected("requires_manual_check","Более 40 вариантов; полный скан не помещается в актуальное окно"); return end
      self.variantKeys,self.variantScans,self.variantIndex=keys,{},0
      self.task.vendor_keys=C:Clone(keys)
      self:NextVendorVariant(item)
    elseif #keys==1 then
      self.task.selected_keys[item]=C:Clone(keys[1])
      self:Select(item,self.token)
    else
      self:Rejected("requires_manual_check","Найдено вариантов: " .. #keys .. ". Нажмите Shopping и выберите вариант " .. C.Items:Name(item) .. " (#" .. item .. ")")
    end
    return
  end
  local commodity = event == "COMMODITY_SEARCH_RESULTS_UPDATED"
  if not commodity and event ~= "ITEM_SEARCH_RESULTS_UPDATED" then return end
  if commodity and ref ~= self.current.item then return end
  if not commodity and not C.Adapter:KeyEqual(ref,self.current.key) then return end
  local full = commodity and C_AuctionHouse.HasFullCommoditySearchResults(ref) or (not commodity and C_AuctionHouse.HasFullItemSearchResults(ref))
  if not full then
    local name = commodity and "RequestMoreCommoditySearchResults" or "RequestMoreItemSearchResults"
    if C_AuctionHouse[name] and C.Adapter:Ready() and self.current.pages<10 then
      self.current.pages=self.current.pages+1
      C.Adapter:Call(name,ref)
    else self:Cancel("Результаты неполные; повторите скан после освобождения AH") end
    return
  end
  local rows = C.Adapter:Results(self.current.item,self.current.key,commodity)
  self.current.received_at=C:Now()
  if self.task.strategy_id=="vendor_arbitrage" then self:VendorRows(rows,commodity,self.token)
  else self:StoreRows(rows,commodity) end
end
local frame = CreateFrame("Frame")
frame:RegisterEvent("ITEM_SEARCH_RESULTS_UPDATED")
frame:RegisterEvent("COMMODITY_SEARCH_RESULTS_UPDATED")
frame:RegisterEvent("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
frame:RegisterEvent("AUCTION_HOUSE_BROWSE_RESULTS_ADDED")
frame:RegisterEvent("AUCTION_HOUSE_BROWSE_FAILURE")
frame:SetScript("OnEvent",function(_,event,...) S:OnEvent(event,...) end)
