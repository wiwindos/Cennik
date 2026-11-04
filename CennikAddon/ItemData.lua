local _, C = ...
function C:ClientLocale()
  return GetLocale and GetLocale() or nil
end
function C:RecipeNames(value)
  value.locale=self:ClientLocale();value.item_names={}
  local get=C_Item and C_Item.GetItemInfo or GetItemInfo
  if type(get)=="function" then
    local function add(id)
      if type(id)~="number" or id<=0 then return end
      local name=get(id)
      if name and name~="" then value.item_names[tostring(id)]=name end
    end
    add(value.item_id)
    for _,line in ipairs(value.inputs or {}) do add(line.item_id) end
    value.item_name=value.item_id and value.item_names[tostring(value.item_id)] or nil
  end
  return value
end
function C:ObservedModernRecipe(id,name)
  local result=self:RecipeNames({spell_id=id,name=name})
  if not (C_TradeSkillUI and C_TradeSkillUI.GetRecipeSchematic) then return result end
  local ok,s=pcall(C_TradeSkillUI.GetRecipeSchematic,id,false)
  if not ok or type(s)~="table" or s.isRecraft or not C:Integer(s.outputItemID)
    or not C:Integer(s.quantityMin) or not C:Integer(s.quantityMax) or s.quantityMax<s.quantityMin then return result end
  local inputs={}
  for _,slot in ipairs(s.reagentSlotSchematics or {}) do
    if slot.required or slot.reagentType==1 then
      if type(slot.reagents)~="table" or #slot.reagents~=1 or not C:Integer(slot.reagents[1].itemID)
        or not C:Integer(slot.quantityRequired) or (slot.variableQuantities and #slot.variableQuantities>0) then return result end
      inputs[#inputs+1]={item_id=slot.reagents[1].itemID,quantity=slot.quantityRequired}
    elseif slot.required==nil and slot.reagentType==nil then return result end
  end
  if #inputs==0 then return result end
  result.item_id=s.outputItemID;result.qty_min=s.quantityMin;result.qty_max=s.quantityMax;result.inputs=inputs
  return self:RecipeNames(result)
end
-- Client-local names/links are presentation only. Imported tasks and identities stay unchanged.
local I = {pending={},failed={},timeout=15}
C.Items = I
function I:Read(item)
  local get = C_Item and C_Item.GetItemInfo or GetItemInfo
  if type(get)~="function" or not C:Integer(item) then return nil end
  local name,link,_,_,_,_,_,_,_,icon = get(item)
  if name and name~="" then return {id=item,name=name,link=link,icon=icon} end
end
function I:RefreshLater()
  if self.refreshQueued then return end
  self.refreshQueued=true
  C_Timer.After(0,function() I.refreshQueued=false; C:Refresh() end)
end
function I:Finish(item,request,info,reason)
  if self.pending[item]~=request then return end
  self.pending[item]=nil
  self.failed[item]=not info and reason or nil
  for _, callback in ipairs(request.callbacks) do callback(info,reason) end
  self:RefreshLater()
end
function I:Poll(item,request)
  if self.pending[item]~=request then return end
  local info=self:Read(item)
  if info then self:Finish(item,request,info); return end
  if GetTime()>=request.deadline then
    self:Finish(item,request,nil,"Не удалось загрузить данные предмета #" .. item .. "; повторите поиск")
    return
  end
  C_Timer.After(0.25,function() I:Poll(item,request) end)
end
function I:Load(item,callback)
  if not C:Integer(item) then if callback then callback(nil,"Неверный ID предмета") end; return end
  local info=self:Read(item)
  if info then if callback then callback(info) end; return end
  local request=self.pending[item]
  if request then if callback then table.insert(request.callbacks,callback) end; return end
  self.failed[item]=nil
  request={deadline=GetTime()+self.timeout,callbacks={}}
  if callback then table.insert(request.callbacks,callback) end
  self.pending[item]=request
  if C_Item and type(C_Item.RequestLoadItemDataByID)=="function" then
    pcall(C_Item.RequestLoadItemDataByID,item)
  end
  self:Poll(item,request)
end
function I:Get(item)
  local info=self:Read(item)
  if info then return info end
  if not self.pending[item] and not self.failed[item] then self:Load(item) end
end
function I:Name(item)
  local info=self:Get(item)
  if info then return info.name end
  return "Предмет #" .. tostring(item) .. (self.pending[item] and " (загрузка…)" or " (не загружен)")
end
function I:Label(item)
  local info=self:Get(item)
  if not info then return self:Name(item) end
  return (info.icon and "|T" .. info.icon .. ":16:16:0:0|t " or "") .. info.name
end
function I:Link(item)
  local info=self:Get(item)
  return info and info.link or self:Name(item)
end
local frame=CreateFrame("Frame")
frame:RegisterEvent("GET_ITEM_INFO_RECEIVED")
frame:RegisterEvent("ITEM_DATA_LOAD_RESULT")
frame:SetScript("OnEvent",function(_,_,item,success)
  local request=I.pending[item]
  if not request then return end
  local info=I:Read(item)
  if info then I:Finish(item,request,info)
  elseif success==false then I:Finish(item,request,nil,"Клиент не загрузил предмет #" .. item .. "; повторите поиск") end
end)

-- NPC price sources share this existing load unit so /reload needs no new TOC file.
do
local _, C = ...
local N={maxAge=900}
C.NPC=N
function N:Scope() return UnitName("player") .. "/" .. GetRealmName() .. "/" .. (UnitFactionGroup("player") or "") end
function N:Key(key) return key.itemID .. ":" .. (key.itemLevel or 0) .. ":" .. (key.itemSuffix or 0) .. ":" .. (key.battlePetSpeciesID or 0) end
function N:Vendor(task,row)
  local sell=C.Adapter:VendorPrice(row)
  if sell~=nil then return sell,"Игровой клиент: конкретный предмет" end
  local manual=task.vendor_prices and task.vendor_prices[self:Key(row.item_key)]
  if manual and manual.scope==self:Scope() and manual.observed_at<=C:Now() and C:Now()-manual.observed_at<=self.maxAge and C:Integer(manual.price,true) then return manual.price,"NPC-цена продажи подтверждена игроком" end
  local base=task.opportunity.details.vendor_sell_copper
  if row.item_id==task.item_id and C:Integer(base) then return base,"База: цена продажи NPC для ID предмета" end
end
function N:Quote(task,item)
  local quote=task.npc_prices and task.npc_prices[item]
  local merchant=C.db.npc_quotes and C.db.npc_quotes[item]
  if merchant and (not quote or merchant.observed_at>quote.observed_at) then quote=merchant end
  if quote and quote.blocked and quote.scope==self:Scope() and C:Now()-quote.observed_at<=self.maxAge then return nil end
  if quote and quote.scope==self:Scope() and quote.observed_at<=C:Now() and C:Now()-quote.observed_at<=self.maxAge
    and C:Integer(quote.batch_quantity) and C:Integer(quote.batch_price_copper,true) then return quote end
  -- Direct resale needs a fresh scoped price, not an assumed database fallback.
  if task.strategy_id=="npc_to_auction" then return nil end
  -- User-authorized stable NPC buy-price fallback from the frozen database snapshot.
  -- Never substitute an AH price or a mutable purchase cap for an NPC price.
  for _,material in ipairs(task.opportunity.details.materials or {}) do
    if material.item_id==item and material.source=="npc" and C:Integer(material.unit_price_copper) then
      return {unit_price_copper=material.unit_price_copper,batch_price_copper=material.unit_price_copper,
        batch_quantity=1,source="База: цена NPC",from_database=true}
    end
  end
end
function N:Cost(quote,quantity)
  local cost=math.ceil(quantity/quote.batch_quantity)*quote.batch_price_copper
  return C:Integer(cost,true) and cost or nil
end
function N:Manual(task,item,price,quantity)
  quantity=quantity or 1
  if C.db.pending or not C:Integer(price,true) or not C:Integer(quantity) then return false end
  task.npc_prices=task.npc_prices or {}
  task.npc_prices[item]={unit_price_copper=math.ceil(price/quantity),batch_price_copper=price,batch_quantity=quantity,
    observed_at=C:Now(),scope=self:Scope(),source="Цена подтверждена игроком"}
  return true
end
function N:ManualVendor(task,key,price)
  if C.db.pending or not C:Integer(price,true) then return false end
  task.vendor_prices=task.vendor_prices or {}
  task.vendor_prices[self:Key(key)]={price=price,scope=self:Scope(),observed_at=C:Now()}
  return true
end
function N:Capture()
  if not C.db or not GetNumMerchantItems or not GetMerchantItemLink then return end
  C.db.npc_quotes=C.db.npc_quotes or {}
  for index=1,GetNumMerchantItems() do
    local link=GetMerchantItemLink(index)
    local item=link and tonumber(link:match("item:(%d+)"))
    local price,quantity,available,purchasable,extended
    if GetMerchantItemInfo then
      local name,texture,usable
      name,texture,price,quantity,available,purchasable,usable,extended=GetMerchantItemInfo(index)
    elseif C_MerchantFrame and C_MerchantFrame.GetItemInfo then
      local info=C_MerchantFrame.GetItemInfo(index)
      if info then price,quantity,available,purchasable,extended=info.price,info.stackCount,info.numAvailable,info.isPurchasable,info.hasExtendedCost end
    end
    if item and C:Integer(price,true) and C:Integer(quantity) and purchasable~=false and extended~=true then
      local itemInfo=C.Items:Read(item)
      C.db.npc_quotes[item]={batch_price_copper=price,batch_quantity=quantity,unit_price_copper=math.ceil(price/quantity),
        available=available and available>=0 and available or nil,observed_at=C:Now(),scope=self:Scope(),source="Открытый торговец",name=itemInfo and itemInfo.name,locale=C:ClientLocale()}
    elseif item then C.db.npc_quotes[item]={blocked=true,observed_at=C:Now(),scope=self:Scope()} end
    if item and C.CollectorStatus and C.CollectorStatus:IsEnabled() and C.db.npc_quotes[item]
      and C:MarketBindingMatches(C.db.collector and C.db.collector.market or C.db.current_market) then
      local fields={};for f in (UnitGUID and UnitGUID("npc") or ""):gmatch("[^-]+") do fields[#fields+1]=f end
      local npc=tonumber(fields[6]);local key=tostring(npc or "unknown") .. ":" .. item
      C.db.collector_npc=C.db.collector_npc or {};C.db.collector_npc_count=C.db.collector_npc_count or 0
      if C.db.collector_npc[key] or C.db.collector_npc_count<5000 then
        if not C.db.collector_npc[key] then C.db.collector_npc_count=C.db.collector_npc_count+1 end
        local quote=C.db.npc_quotes[item]
        C.db.collector_observation_sequence=(C.db.collector_observation_sequence or 0)+1
        C.db.collector_npc[key]={item_id=item,npc_id=npc,npc_name=npc and UnitName("npc") or nil,
          locale=C:ClientLocale(),item_name=quote.name,
          observation_id=C.db.device_id .. ":observation:" .. C.db.collector_observation_sequence,
          map_id=C_Map and C_Map.GetBestMapForUnit and C_Map.GetBestMapForUnit("player"),
          batch_price_copper=quote.batch_price_copper,batch_quantity=quote.batch_quantity,
          available=quote.available,blocked=quote.blocked,observed_at=math.floor(C:Now())}
      elseif C.CollectorStatus then C.CollectorStatus:Set("OBSERVATION_LIMIT") end
    end
  end
  C:Refresh()
end
local frame=CreateFrame("Frame")
frame:RegisterEvent("MERCHANT_SHOW"); frame:RegisterEvent("MERCHANT_UPDATE")
frame:SetScript("OnEvent",function() N:Capture() end)

end
