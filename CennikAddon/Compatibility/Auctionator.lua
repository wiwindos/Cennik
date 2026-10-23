local _, C = ...
local A = {externalUntil=0, sending=false}
C.Adapter = A
function A:Open() return AuctionHouseFrame and AuctionHouseFrame:IsShown() end
function A:Ready(owner)
  return not self.error and self:Open() and not self.shopping and not self.fullScan
    and (owner=="sniper" or not (C.Sniper and C.Sniper.current))
    and not (C.MarketCollector and C.MarketCollector.packing)
    and GetTime() >= self.externalUntil and C_AuctionHouse.IsThrottledMessageSystemReady()
    and (not Auctionator.AH.Queue or not Auctionator.AH.Queue.queue or #Auctionator.AH.Queue.queue == 0)
end
function A:Initialize()
  local api = C_AuctionHouse
  local required = {"MakeItemKey","SendSearchQuery","GetItemKeyInfo","GetNumCommoditySearchResults",
    "GetCommoditySearchResultInfo","GetNumItemSearchResults","GetItemSearchResultInfo",
    "HasFullCommoditySearchResults","HasFullItemSearchResults","IsThrottledMessageSystemReady",
    "StartCommoditiesPurchase","ConfirmCommoditiesPurchase","CancelCommoditiesPurchase","PlaceBid"}
  for _, name in ipairs(required) do
    if not api or type(api[name]) ~= "function" then self.error = "Не поддерживается игровой API: " .. name; return end
  end
  if not Auctionator or not Auctionator.API or not Auctionator.API.v1 or not Auctionator.API.v1.MultiSearch then
    self.error = "Нужен Auctionator 340 с публичным API v1"; return
  end
  for _, name in ipairs({"SendSearchQuery","SendSellSearchQuery","SendBrowseQuery","SearchForItemKeys","RequestMoreBrowseResults","ReplicateItems","StartCommoditiesPurchase","ConfirmCommoditiesPurchase","PlaceBid"}) do
    if api[name] then hooksecurefunc(api,name,function()
      if not A.sending then
        A.externalUntil = GetTime() + 3
        if C.db.pending and (C.db.pending.stage == "buying" or C.db.pending.stage == "unknown") then
          C.db.pending.external_conflict = true
          if C.db.pending.sniper then C.Sniper:PurchaseUnknown("Другая операция AH: результат нужно уточнить")
          else C:State(C.db.tasks[C.db.pending.task_id],"requires_manual_check","Другая операция AH: автоматическая привязка результата запрещена") end
        end
        if C.Scanner.active then C.Scanner:Cancel("Пользователь или другой аддон начал операцию AH") end
        if C.db.pending and (C.db.pending.stage == "quoted" or C.db.pending.stage == "await_quote") then C.Execution:Stop() end
      end
    end) end
  end
  -- API v1 cannot expose activity/results: the adapter uses audited 340 events.
  if Auctionator.EventBus then
    local events = Auctionator.Shopping.Tab.Events
    self.startEvent,self.endEvent = events.SearchStart,events.SearchEnd
    local full = Auctionator.FullScan and Auctionator.FullScan.Events
    self.fullStart,self.fullEnd,self.fullFailed = full and full.ScanStart,full and full.ScanComplete,full and full.ScanFailed
    local names = {self.startEvent,self.endEvent}
    if full then table.insert(names,self.fullStart); table.insert(names,self.fullEnd); table.insert(names,self.fullFailed) end
    Auctionator.EventBus:Register(self,names)
  end
end
function A:LotStillAvailable(lot)
  if lot.commodity then return true end
  if not C_AuctionHouse.GetAuctionInfoByID then return false end
  local live=C_AuctionHouse.GetAuctionInfoByID(lot.auction_id)
  return live and live.buyoutAmount==lot.cost and self:ItemLotQuantity(live)==lot.quantity and live.containsOwnerItem==false
    and not live.containsAccountItem and self:KeyEqual(live.itemKey,lot.key)
end
function A:ReceiveEvent(event)
  if event == self.startEvent then
    self.shopping = true
    if C.Sniper then C.Sniper:HoldForManualSearch("Поиск Auctionator") end
  elseif event == self.endEvent then self.shopping = false
  elseif event == self.fullStart then self.fullScan = true
  elseif event == self.fullEnd or event == self.fullFailed then self.fullScan = false end
end
function A:Call(name,...)
  self.sending = true
  local ok, result = pcall(C_AuctionHouse[name],...)
  self.sending = false
  return ok,result
end
function A:Equipment(item)
  local get = C_Item and C_Item.GetItemInfoInstant or GetItemInfoInstant
  if not get then return nil end
  local id, _, _, equip, _, class = get(item)
  if not id then return nil end
  -- Class IDs are stable across client locales. NON_EQUIP is not equipment.
  if type(class)=="number" then return class == 2 or class == 4 end
  return not not (equip and equip ~= "" and equip ~= "INVTYPE_NON_EQUIP" and equip ~= "NON_EQUIP")
end
function A:Shopping(item)
  if not C:Integer(item) then return false,"Неверный ID предмета" end
  if not self:Open() then return false,"Откройте аукцион" end
  self.shoppingToken=(self.shoppingToken or 0)+1
  local token=self.shoppingToken
  local finished,started,message=false,false,"Загрузка предмета #" .. item .. " для поиска…"
  C.UI.notice=message; C:Refresh()
  C.Items:Load(item,function(info,reason)
    if token~=A.shoppingToken then return end
    finished=true
    if not info then message=reason
    elseif not A:Open() then message="Аукцион закрыт; поиск не запущен"
    elseif C.db.pending or A.fullScan or A.shopping then message="AH занят; повторите поиск после завершения текущей операции"
    else
      local ok,err=pcall(Auctionator.API.v1.MultiSearch,"Cennik",{info.name})
      started=ok
      message=ok and ("Поиск: " .. info.name .. " (#" .. item .. ")") or ("Ошибка поиска: " .. tostring(err))
    end
    C.UI.notice=message; C:Refresh()
  end)
  return not finished or started,message
end
function A:OpenOffers(key,unitPrice)
  if not self:Open() then return false,"Откройте аукцион" end
  if C.db.pending or C.Scanner.active or not self:Ready("sniper") then
    return false,"Аукцион занят. Дождитесь завершения операции и повторите переход"
  end
  if not key or not C:Integer(key.itemID) or not C_AuctionHouse.GetItemKeyInfo(key) then
    return false,"Данные варианта ещё не загружены. Повторите проверку предмета"
  end
  if type(AuctionHouseFrame.SelectBrowseResult)~="function" then return false,"Клиент не поддерживает переход к предложениям" end
  -- The same native handoff used by Auctionator's item icon. The buy panel
  -- loads current offers itself; opening it never starts a purchase.
  local ok=pcall(function()
    -- Native item/commodity subpanels do not select the Buy tab themselves
    -- after LibAHTab has deselected it for an addon tab.
    if AuctionHouseFrameDisplayMode and AuctionHouseFrameDisplayMode.Buy then
      AuctionHouseFrame:SetDisplayMode(AuctionHouseFrameDisplayMode.Buy)
    end
    AuctionHouseFrame:SelectBrowseResult({itemKey=C:Clone(key),minPrice=C:Integer(unitPrice) and unitPrice or 0})
  end)
  if not ok then return false,"Не удалось открыть предложения в игре. Повторите переход после проверки предмета" end
  return true
end
function A:KeyEqual(a,b)
  return a and b and a.itemID == b.itemID and (a.itemLevel or 0) == (b.itemLevel or 0)
    and (a.itemSuffix or 0) == (b.itemSuffix or 0) and (a.battlePetSpeciesID or 0) == (b.battlePetSpeciesID or 0)
end
function A:VendorPrice(row)
  local get=C_Item and C_Item.GetItemInfo or GetItemInfo
  if not get then return nil end
  local ref=row.item_link
  if not ref then
    if self:Equipment(row.item_id)~=false then return nil end
    ref=row.item_id
  end
  if type(ref)=="string" and tonumber(ref:match("item:(%d+)"))~=row.item_id then return nil end
  local ok,name,link,quality,level,minLevel,class,subclass,stack,equip,icon,sell=pcall(get,ref)
  if ok and name and C:Integer(sell,true) then return sell end
end
function A:StackLimit(item)
  local get=C_Item and C_Item.GetItemInfo or GetItemInfo
  local stack
  if type(get)=="function" then stack=select(8,get(item)) end
  local instant=C_Item and C_Item.GetItemInfoInstant or GetItemInfoInstant
  if not stack and type(instant)=="function" then
    local _,_,_,_,_,class=instant(item)
    if class==1 then stack=1 end -- bags/containers cannot form a physical stack
  end
  return C:Integer(stack) and stack or nil
end
function A:ItemLotQuantity(info)
  if not info or not C:Integer(info.quantity) then return nil end
  -- Forever's item path buys one represented AuctionID, even without item cache.
  -- quantity in the search row describes available units at that price.
  return 1
end
function A:Results(item,key,commodity)
  local rows = {}
  self.resultDiagnostic={item_id=item,commodity=commodity,at=C:Now(),grouped_single_rows=0,quantity_rejected=0,owned_rows=0,unknown_owner_rows=0}
  local stack=not commodity and self:StackLimit(item) or nil
  local count = commodity and C_AuctionHouse.GetNumCommoditySearchResults(item) or C_AuctionHouse.GetNumItemSearchResults(key)
  self.resultDiagnostic.total_rows=count
  for i=1,count do
    local info = commodity and C_AuctionHouse.GetCommoditySearchResultInfo(item,i) or C_AuctionHouse.GetItemSearchResultInfo(key,i)
    if info then
      local searchQuantity=info.quantity
      local auctionID=info.auctionID
      local live
      if i==1 and not commodity and info.auctionID and C_AuctionHouse.GetAuctionInfoByID then
        live=C_AuctionHouse.GetAuctionInfoByID(info.auctionID)
      end
      -- Prices/depth belong to the completed search, not the separate auction cache.
      -- GetAuctionInfoByID is used to revalidate the chosen action before PlaceBid.
      local quantity
      if commodity then quantity=info.quantity else quantity=self:ItemLotQuantity(info) end
      local groupQuantity=info.quantity
      if not commodity and C:Integer(groupQuantity) and groupQuantity>1 then self.resultDiagnostic.grouped_single_rows=self.resultDiagnostic.grouped_single_rows+1 end
      if i==1 then
        self.resultDiagnostic.search_quantity=searchQuantity; self.resultDiagnostic.lot_quantity=live and live.quantity
        self.resultDiagnostic.auction_id=info.auctionID or auctionID; self.resultDiagnostic.stack_limit=stack
        self.resultDiagnostic.normalized_quantity=quantity
      end
      local invalidQuantity=not commodity and not quantity
      if invalidQuantity then self.resultDiagnostic.quantity_warning="Количество поисковой группы не подтверждает размер конкретного лота"; self.resultDiagnostic.quantity_rejected=self.resultDiagnostic.quantity_rejected+1 end
      local price = commodity and info.unitPrice or info.buyoutAmount
      if C:Integer(price) then self.resultDiagnostic.cheapest_raw_price=math.min(self.resultDiagnostic.cheapest_raw_price or price,price) end
      local owned = commodity and info.numOwnerItems or (info.containsOwnerItem and quantity or 0)
      local known = commodity and type(info.numOwnerItems) == "number" or (not commodity and type(info.containsOwnerItem) == "boolean")
      if not known then self.resultDiagnostic.unknown_owner_rows=self.resultDiagnostic.unknown_owner_rows+1 end
      if (owned or 0)>0 or info.containsAccountItem then self.resultDiagnostic.owned_rows=self.resultDiagnostic.owned_rows+1 end
      if not invalidQuantity and C:Integer(quantity) and C:Integer(price) then
        local available = commodity and math.max(0,quantity-(owned or 0)) or groupQuantity
        table.insert(rows,{item_id=item, item_key=info.itemKey or key, quantity=commodity and available or 1,
          available_quantity=available,purchase_quantity=not commodity and 1 or nil,
          unit_price=price, total_price=commodity and price*available or price,
          auction_id=info.auctionID or auctionID, commodity=commodity, owned=(commodity and available==0) or (not commodity and (owned or 0)>0) or info.containsAccountItem == true,
          ownership_known=known, observed_at=C:Now(), item_link=info.itemLink, search_quantity=searchQuantity})
      end
    end
  end
  table.sort(rows,function(a,b) if a.unit_price == b.unit_price then return (a.auction_id or 0)<(b.auction_id or 0) end return a.unit_price<b.unit_price end)
  self.resultDiagnostic.accepted_rows=#rows
  for _,row in ipairs(rows) do if row.ownership_known and not row.owned then self.resultDiagnostic.cheapest_eligible_unit_price=row.unit_price; break end end
  return rows
end
