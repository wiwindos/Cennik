local _, C = ...
local S=C.Sniper
function S:Catalog()
  local catalog=CennikImport and CennikImport.sniper_catalog
  if not catalog or catalog.workspace_id~=C.db.workspace_id then return {items={},recipes={}} end
  return catalog
end
function S:CatalogItem(item)
  for _,row in ipairs(self:Catalog().items or {}) do if row.item_id==item then return row end end
end
function S:Recipes(item)
  local rows={}
  for _,recipe in ipairs(self:Catalog().recipes or {}) do if recipe.item_id==item then rows[#rows+1]=recipe end end
  return rows
end
local function fold(value)
  local upper={"А","Б","В","Г","Д","Е","Ё","Ж","З","И","Й","К","Л","М","Н","О","П","Р","С","Т","У","Ф","Х","Ц","Ч","Ш","Щ","Ъ","Ы","Ь","Э","Ю","Я"}
  local lower={"а","б","в","г","д","е","ё","ж","з","и","й","к","л","м","н","о","п","р","с","т","у","ф","х","ц","ч","ш","щ","ъ","ы","ь","э","ю","я"}
  value=string.lower(value)
  for i,letter in ipairs(upper) do value=value:gsub(letter,lower[i]) end
  return value
end
function S:Lookup(query,callback)
  query=(query or ""):match("^%s*(.-)%s*$")
  if query=="" or #query>200 then return false,"Введите название, ID или ссылку" end
  local id=tonumber(query) or tonumber(query:match("item:(%d+)"))
  if C:Integer(id) then callback({{item_id=id}}); return true end
  local get=C_Item and C_Item.GetItemInfo or GetItemInfo
  if get then local _,link=get(query); id=link and tonumber(link:match("item:(%d+)")) end
  if C:Integer(id) then callback({{item_id=id}}); return true end
  local matches,seen={},{}
  local needle=fold(query)
  local cached=C_Item and C_Item.IsItemDataCachedByID
  for _,row in ipairs(self:Catalog().items or {}) do
    -- Do not request thousands of uncached item records just to match a name.
    local localName=type(cached)=="function" and cached(row.item_id) and C.Items:Read(row.item_id)
    local name=localName and localName.name or row.name or ""
    if fold(name):find(needle,1,true) or fold(row.name or ""):find(needle,1,true) then
      if not seen[row.item_id] and #matches<100 then matches[#matches+1]={item_id=row.item_id,name=name}; seen[row.item_id]=true end
    end
  end
  if #matches>0 then callback(matches); return true end
  if self.manualHold or self.foreignTab then return false,"Откройте Cennik и нажмите «Продолжить поиск», затем повторите поиск названия" end
  if C.db.pending or C.Scanner.active or not C.Adapter:Ready("sniper") then return false,"Дождитесь завершения другой операции аукциона" end
  if not C_AuctionHouse.SendBrowseQuery or not C_AuctionHouse.GetBrowseResults then return false,"Используйте поиск Auctionator и кнопку «В снайпер»" end
  self:CancelSearch("Поиск предмета по названию")
  self.current={kind="lookup",name=query,callback=callback,started=GetTime(),sent=false,pages=0}
  return true,"Поиск названия на аукционе…"
end
function S:NewDefinition(item,quantity,bags)
  C.db.sniper.sequence=C.db.sniper.sequence+1
  return {id=C.db.device_id .. "-s-" .. C.db.sniper.sequence,market=C.db.current_market,item_id=item,
    quantity=quantity or 1,mode="any",priority="normal",unit_limit_copper=0,discount_bps=5000,use_bags=bags==true}
end
function S:MarkBought(identity,quantity)
  local rule=C.db.sniper.rules[identity]
  if not rule or rule.removed or not C:Integer(quantity) or quantity>self:Remaining(rule) then return false,"Укажите количество от 1 до оставшейся потребности" end
  if C.db.pending or #C.db.sniper.events>=1900 then return false,"Сначала завершите операцию и синхронизируйте журнал" end
  rule.bought=rule.bought+quantity
  if self:Remaining(rule)==0 then rule.enabled=false end
  self.alerts[identity]=nil
  self:Emit("external_purchase",{rule_id=identity,item_id=rule.definition.item_id,quantity=quantity,manually_confirmed=true,
    remaining=self:Remaining(rule),cost_unknown=true})
  self:Notice("Количество учтено вручную; стоимость покупки неизвестна")
  return true
end
function S:InstallAuctionatorButtons()
  if self.auctionatorButtonsInstalled then return end
  local events=Auctionator and Auctionator.Buying and Auctionator.Buying.Events
  if not events or not Auctionator.EventBus then return end
  self.auctionatorButtonsInstalled=true
  local listener={}
  function listener:ReceiveEvent(event,row)
    if not row or not row.itemKey or not C:Integer(row.itemKey.itemID) then return end
    S.auctionatorSelection=C:Clone(row.itemKey)
    if S.ShowAuctionatorShortcut then S:ShowAuctionatorShortcut(row.itemKey.itemID) end
  end
  self.auctionatorListener=listener
  Auctionator.EventBus:Register(listener,{events.ShowItemBuy,events.ShowCommodityBuy})
  -- Capture right/left selection as well as the buying screen, without editing Auctionator files.
  if AuctionatorShoppingResultsRowMixin and AuctionatorShoppingResultsRowMixin.OnClick then
    hooksecurefunc(AuctionatorShoppingResultsRowMixin,"OnClick",function(row)
      if row.rowData then listener:ReceiveEvent("selection",row.rowData) end
    end)
  end
end
