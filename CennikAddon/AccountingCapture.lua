local _, C = ...
local L=C.Accounting
local function hook(api,name,fn)
  if api and type(api[name])=="function" then hooksecurefunc(api,name,function(...) if L.db then fn(...) end end) end
end
local function moneyInfo(link)
  local info=C_Item and C_Item.GetItemInfo or GetItemInfo
  if info then return select(11,info(link)) end
end
function L:MerchantSnapshot()
  self.merchant={}
  if GetNumMerchantItems and GetMerchantItemInfo and GetMerchantItemLink then
    for index=1,(GetNumMerchantItems() or 0) do
      local _,_,price,pack,_,_,_,extended=GetMerchantItemInfo(index)
      local line=self:Line(GetMerchantItemLink(index),1)
      if line and C:Integer(price,true) and C:Integer(pack) and not extended then
        self.merchant[self:Key(line)]={price=price,pack=pack}
      end
    end
  end
  self.bags=self:Snapshot()
end
function L:VendorIntent()
  if not self.merchant or not self.bags then return end
  self.vendorBefore=self.vendorBefore or C:Clone(self.bags)
  self.vendorAttempts=0
  self.vendorToken=(self.vendorToken or 0)+1; local token=self.vendorToken
  self.db.interrupted={kind="vendor",observed_at=math.floor(C:Now())}
  C_Timer.After(.8,function() if L.vendorToken==token then L:VendorFinish() end end)
end
function L:VendorFinish()
  if not self.vendorBefore then return end
  local after=self:Snapshot(); if not after then return end
  local before=self.vendorBefore
  local changes=self:Delta(before,after)
  local expected,operations,valid=0,{},true
  for _,change in ipairs(changes) do
    local line=change.line; local quantity=math.abs(change.delta); line.quantity=quantity
    local cost
    if change.delta>0 then
      local price=self.merchant[self:Key(line)]
      if price then cost=quantity/price.pack*price.price end
    else local unit=moneyInfo(line.link~="" and line.link or "item:" .. line.item_id); if C:Integer(unit,true) then cost=unit*quantity end end
    if not C:Integer(cost,true) then valid=false else
      expected=expected+(change.delta>0 and -cost or cost)
      operations[#operations+1]={kind=change.delta>0 and "purchase" or "sale",line=line,cost=cost}
    end
  end
  if not (#changes>0 and valid and expected==after.money-before.money) and (self.vendorAttempts or 0)<3 then
    self.vendorAttempts=(self.vendorAttempts or 0)+1
    C_Timer.After(.5,function() L:VendorFinish() end); return
  end
  self.vendorBefore=nil; self.db.interrupted=nil
  if #changes>0 and valid and expected==after.money-before.money then
    for _,op in ipairs(operations) do
      local fields=self:Context(op.line.item_id,op.kind=="sale")
      fields.source="vendor"; fields.money_copper=op.cost
      fields[op.kind=="purchase" and "outputs" or "inputs"]={op.line}
      self:Emit(op.kind,fields)
    end
  elseif #changes>0 or after.money~=before.money then
    self:Issue("Не удалось однозначно подтвердить покупку/продажу NPC",{money_delta=after.money-before.money,changes=changes})
  end
  self.bags=after
  if self.merchantClosing then self.merchant=nil; self.merchantClosing=nil end
end
function L:RecipeIntent(recipe,index)
  local inputs,output={},nil
  if index and GetTradeSkillNumReagents and GetTradeSkillReagentItemLink and GetTradeSkillReagentInfo and GetTradeSkillItemLink then
    for i=1,(GetTradeSkillNumReagents(index) or 0) do
      local _,_,count=GetTradeSkillReagentInfo(index,i)
      local line=self:Line(GetTradeSkillReagentItemLink(index,i),count)
      if not line then return end; inputs[line.item_id]=true
    end
    output=self:Line(GetTradeSkillItemLink(index),1)
    local link=GetTradeSkillRecipeLink and GetTradeSkillRecipeLink(index)
    recipe=link and tonumber(link:match("enchant:(%d+)") or link:match("spell:(%d+)")) or recipe
  elseif C_TradeSkillUI and C_TradeSkillUI.GetRecipeSchematic and C_TradeSkillUI.GetRecipeItemLink then
    local schematic=C_TradeSkillUI.GetRecipeSchematic(recipe,false)
    if not schematic then return end
    for _,slot in ipairs(schematic.reagentSlotSchematics or {}) do
      for _,reagent in ipairs(slot.reagents or {}) do if reagent.itemID then inputs[reagent.itemID]=true end end
    end
    output=self:Line(C_TradeSkillUI.GetRecipeItemLink(recipe),1)
  end
  if not output or not next(inputs) then self:Issue("Не удалось прочитать состав рецепта",{recipe_id=recipe}); return end
  if self.transform then self:TransformFinish() end
  self.transform={kind="craft",recipe=recipe,allowedInputs=inputs,allowedOutputs={[output.item_id]=true},
    before=self:Snapshot(),started=GetTime(),success=0}
  self.db.interrupted={kind="craft",recipe_id=recipe}
end
function L:TransformFinish()
  local p=self.transform
  if not p or not p.before then return end
  if p.kind=="disenchant" and not p.lootClosed then return end
  local after=self:Snapshot(); if not after then return end
  local inputs,outputs,valid={}, {}, true
  for _,change in ipairs(self:Delta(p.before,after)) do
    local line=change.line; line.quantity=math.abs(change.delta)
    if change.delta<0 then
      if p.allowedInputs and not p.allowedInputs[line.item_id] then valid=false end
      inputs[#inputs+1]=line
    else
      if p.allowedOutputs and not p.allowedOutputs[line.item_id] then valid=false end
      outputs[#outputs+1]=line
    end
  end
  if (#inputs==0 or #outputs==0) and valid and p.success>0 and GetTime()-(p.lastSuccess or p.started)<5 then
    C_Timer.After(.5,function() if L.transform==p then L:TransformFinish() end end); return
  end
  if #inputs==0 and #outputs==0 then return end
  if p.kind=="disenchant" and (#inputs~=1 or inputs[1].quantity~=1) then valid=false end
  if valid and #inputs>0 and #outputs>0 and p.success>0 then
    local fields=self:Context(p.kind=="craft" and outputs[1].item_id or inputs[1].item_id,p.kind=="craft")
    fields.inputs,fields.outputs,fields.source=inputs,outputs,"bag_delta_and_spell"
    fields.details={recipe_id=p.recipe,successful_casts=p.success}
    self:Emit(p.kind,fields)
    p.before=after; p.success=0
    self.db.interrupted=nil
    if p.kind=="disenchant" then self.transform=nil end
  else
    self:Issue("Изменения сумок не удалось однозначно связать с " .. (p.kind=="craft" and "крафтом" or "распылением"),
      {kind=p.kind,recipe_id=p.recipe,inputs=inputs,outputs=outputs,successful_casts=p.success})
    self.transform=nil; self.db.interrupted=nil
  end
  self.bags=after
end
function L:ScheduleTransform()
  local p=self.transform; if not p then return end
  self.transformToken=(self.transformToken or 0)+1; local token=self.transformToken
  C_Timer.After(.6,function()
    if L.transform==p and L.transformToken==token then L:TransformFinish() end
  end)
end
function L:ReadInbox()
  local result={}
  if not GetInboxNumItems or not GetInboxInvoiceInfo or not GetInboxHeaderInfo then return result end
  for index=1,(GetInboxNumItems() or 0) do
    local _,_,sender,subject,money,cod,_,count=GetInboxHeaderInfo(index)
    local kind,name,other,bid,buyout,deposit,fee=GetInboxInvoiceInfo(index)
    local invoice=GetInboxText and select(5,GetInboxText(index))
    local items,byKey={},{}
    for slot=1,(ATTACHMENTS_MAX_RECEIVE or 16) do
      local link=GetInboxItemLink and GetInboxItemLink(index,slot)
      local quantity=GetInboxItem and select(4,GetInboxItem(index,slot))
      local line=self:Line(link,quantity)
      if line then
        local key=self:Key(line)
        if byKey[key] then byKey[key].quantity=byKey[key].quantity+line.quantity
        else byKey[key]=line;items[#items+1]=line end
      end
    end
    local parts={sender or "",subject or "",kind or "",name or "",other or "",money or 0,bid or 0,buyout or 0,deposit or 0,fee or 0,count or 0}
    local collected={sender or "",subject or "",money or 0,cod or 0}
    for _,line in ipairs(items) do parts[#parts+1]=self:Key(line) .. ":" .. line.quantity end
    for _,line in ipairs(items) do collected[#collected+1]=self:Key(line) .. ":" .. line.quantity end
    result[index]={fingerprint=table.concat(parts,"|"),header_key=(sender or "") .. "|" .. (subject or ""),collection_key=table.concat(collected,"|"),kind=kind,name=name,subject=subject,is_invoice=invoice,money=money or 0,cod=cod or 0,
      bid=bid,buyout=buyout,deposit=deposit,fee=fee,items=items,sender=sender}
  end
  return result
end
function L:MailIntent(index,mode)
  local mail=self.inbox and self.inbox[index]
  if not mail or mail.cod>0 then return end
  self.mailPending=self.mailPending or {}; self.db.mail_pending=self.mailPending
  local id=mail.collection_key .. ":" .. index
  if not self.mailPending[id] then
    local count=0; for _,other in pairs(self.inbox) do if other.collection_key==mail.collection_key then count=count+1 end end
    local headerCount=0;for _,other in pairs(self.inbox) do if other.header_key==mail.header_key then headerCount=headerCount+1 end end
    self.mailPending[id]={mail=C:Clone(mail),count=count,mode=mode,header_count=headerCount}
  end
  C_Timer.After(.5,function() if L.db then L:InboxUpdate() end end)
  local pending=self.mailPending[id]
  C_Timer.After(10,function()
    if L.mailPending and L.mailPending[id]==pending then
      L:Issue("Получение почты не удалось подтвердить",{mail=pending.mail}); L.mailPending[id]=nil
    end
  end)
end
function L:ResolveSoldItem(mail)
  local candidates={}
  local gross=mail.buyout and mail.buyout>0 and mail.buyout or mail.bid
  if not C:Integer(gross,true) then return end
  for id,row in pairs(self.db.listings or {}) do
    if not row.resolved and row.name==mail.name and row.character==UnitName("player") and row.realm==GetRealmName()
      and C:Integer(row.total) then
      local quantity=gross*row.line.quantity/row.total
      if C:Integer(quantity) and quantity<=(row.remaining or row.line.quantity) then
        candidates[#candidates+1]={id=id,row=row,quantity=quantity}
      end
    end
  end
  -- Identical variants use the same FIFO rule as the cost ledger. Different
  -- quantities/variants cannot be inferred from a name-only invoice.
  if #candidates>0 then
    local first=candidates[1]
    for _,other in ipairs(candidates) do
      if self:Key(first.row.line)~=self:Key(other.row.line) or first.quantity~=other.quantity then return end
    end
    table.sort(candidates,function(a,b) return (a.row.created or 0)<(b.row.created or 0) end)
    local candidate=candidates[1]; local row=candidate.row
    local line=C:Clone(row.line); line.quantity=candidate.quantity
    return line,candidate.id
  end
  return nil
end
function L:CollectedMail(mail,mode)
  if C.Production and C.Production:MailReceived(mail) then return end
  if mail.kind=="seller" and mail.money>0 and mode~="item" then
    local line,listing=self:ResolveSoldItem(mail)
    local gross=mail.buyout and mail.buyout>0 and mail.buyout or mail.bid
    if line and C:Integer(gross,true) and C:Integer(mail.fee,true) and C:Integer(mail.deposit,true)
      and mail.money==gross-mail.fee+mail.deposit then
      local saved=self:Emit("sale",{source="auction_mail",inputs={line},money_copper=gross-mail.fee,
        returned_deposit_copper=mail.deposit,listing_id=listing,details={gross_copper=gross,commission_copper=mail.fee}})
      if saved then
        local row=self.db.listings[listing]; row.remaining=(row.remaining or row.line.quantity)-line.quantity; row.resolved=row.remaining==0
      end
    else self:Issue("Продажа AH подтверждена почтой; предмет/количество или комиссия требуют сверки",{mail=mail}) end
  elseif mail.kind=="buyer" and #mail.items==1 and mode~="money" then
    local line=mail.items[1]; local cost=mail.buyout and mail.buyout>0 and mail.buyout or mail.bid
    if not C:Integer(cost,true) then self:Issue("Неизвестна стоимость покупки из почты",{mail=mail}); return end
    local remaining,total=line.quantity,cost
    for _,receipt in ipairs(self.db.receivable) do
      if receipt.item_id==line.item_id and receipt.character==UnitName("player") and receipt.realm==GetRealmName()
        and receipt.quantity>0 and receipt.cost*line.quantity==cost*receipt.quantity then
        local take=math.min(remaining,receipt.quantity); local amount=receipt.cost*take/receipt.quantity
        remaining=remaining-take; total=total-amount; receipt.quantity=receipt.quantity-take; receipt.cost=receipt.cost-amount
        if remaining==0 then break end
      end
    end
    if remaining>0 then
      line.quantity=remaining
      self:Emit("purchase",{source="auction_mail",outputs={line},money_copper=total})
    else self:Emit("receipt",{source="auction_mail",outputs={line},reason="Получение ранее учтённой покупки; повторного расхода нет"}) end
  elseif #mail.items>0 then
    for _,line in ipairs(mail.items) do
      local expired=AUCTION_EXPIRED_MAIL_SUBJECT and string.format(AUCTION_EXPIRED_MAIL_SUBJECT,line.item_name)
      local cancelled=AUCTION_REMOVED_MAIL_SUBJECT and string.format(AUCTION_REMOVED_MAIL_SUBJECT,line.item_name)
      local matches={}
      for id,row in pairs(self.db.listings or {}) do
        if not row.resolved and row.character==UnitName("player") and row.realm==GetRealmName()
          and row.line.item_id==line.item_id and (row.remaining or row.line.quantity)>=line.quantity
          and row.returned then matches[#matches+1]=id end
      end
      if #matches==1 and self.db.listings[matches[1]].returned then
        local subject=mail.subject
        if subject==expired or subject==cancelled then
          local saved=self:Emit("return",{source="auction_mail",listing_id=matches[1],outputs={line},reason="Получен отменённый или истёкший лот"})
          if saved then
            local row=self.db.listings[matches[1]]; row.remaining=(row.remaining or row.line.quantity)-line.quantity; row.resolved=row.remaining==0
          end
        else self:Issue("Не подтверждён отправитель возвращённого лота",{mail=mail}) end
      elseif mail.kind~=nil or mail.is_invoice or mail.subject==expired or mail.subject==cancelled then
        self:Issue("Неоднозначная почта AH: требуется сверка",{mail=mail})
      else
        local remaining=line.quantity
        local credits=self.db.transfer_receivable or {}
        for _,credit in ipairs(credits) do
          if credit.target==UnitName("player") and credit.realm==GetRealmName() and credit.sender==mail.sender
            and self:Key(credit.line)==self:Key(line) then
            local count=math.min(remaining,credit.line.quantity); remaining=remaining-count;credit.line.quantity=credit.line.quantity-count
          end
        end
        if remaining>0 then
          local unknown=C:Clone(line);unknown.quantity=remaining
          self:Emit("opening",{source="mail_untracked",outputs={unknown},reason="Получение вне учтённой покупки/переноса: себестоимость неизвестна"})
        else self:Emit("receipt",{source="own_mail",outputs={line},reason="Получен ранее учтённый перенос; себестоимость сохранена"}) end
      end
    end
  end
end
function L:InboxUpdate()
  local inbox=self:ReadInbox(); local counts={}
  for _,mail in pairs(inbox) do counts[mail.collection_key]=(counts[mail.collection_key] or 0)+1 end
  local used={}
  for id,pending in pairs(self.mailPending or {}) do
    local fingerprint=pending.mail.collection_key
    for _,mail in pairs(inbox) do
      if mail.collection_key==fingerprint and mail.kind then pending.mail=C:Clone(mail) end
    end
    local removed=pending.count-(counts[fingerprint] or 0)
    if removed>(used[fingerprint] or 0) then
      local collected=C:Clone(pending.mail)
      local matching={};for _,mail in pairs(inbox) do if mail.header_key==pending.mail.header_key then matching[#matching+1]=mail end end
      if pending.header_count==1 and #matching==1 then
        local left={};for _,line in ipairs(matching[1].items) do left[self:Key(line)]=line.quantity end
        collected.items={}
        for _,line in ipairs(pending.mail.items) do
          local q=line.quantity-(left[self:Key(line)] or 0)
          if q>0 then local row=C:Clone(line);row.quantity=q;collected.items[#collected.items+1]=row end
        end
        collected.money=math.max(0,pending.mail.money-matching[1].money)
      end
      self:CollectedMail(collected,pending.mode); self.mailPending[id]=nil
      used[fingerprint]=(used[fingerprint] or 0)+1
    end
  end
  self.inbox=inbox
end
function L:PostIntent(location,duration,quantity,price,commodity)
  if not C_Item or not C_Item.GetItemLink then return end
  local line=self:Line(C_Item.GetItemLink(location),quantity); if not line or not C:Integer(price,true) then return end
  local api=C_AuctionHouse
  local deposit=commodity and api.CalculateCommodityDeposit and api.CalculateCommodityDeposit(line.item_id,duration,quantity)
    or not commodity and api.CalculateItemDeposit and api.CalculateItemDeposit(location,duration,quantity)
  self.posting={line=line,deposit=deposit,total=commodity and price*quantity or price,
    before=self:Snapshot(),started=GetTime()}
end
function L:Posted(auctionID)
  local p=self.posting
  if not p or not C:Integer(auctionID) or GetTime()-p.started>30 then return end
  local id=tostring(auctionID)
  self.db.listings=self.db.listings or {}
  local key=self:Key(p.line); local after=self:Snapshot()
  local beforeQuantity=p.before and p.before.items[key] and p.before.items[key].quantity or 0
  local afterQuantity=after and after.items[key] and after.items[key].quantity or 0
  if not after or beforeQuantity-afterQuantity~=p.line.quantity then
    self:Issue("Выставление AH подтверждено; количество требует сверки",{auction_id=id,line=p.line}); self.posting=nil; return
  end
  local paid=p.before.money-after.money
  local deposit=paid==p.deposit and paid or nil
  self.db.listings[id]={line=p.line,name=p.line.item_name,deposit=deposit,total=p.total,remaining=p.line.quantity,created=math.floor(C:Now()),
    character=UnitName("player"),realm=GetRealmName()}
  self:Emit("listing",{source="auction",outputs={p.line},listing_id=id,deposit_copper=deposit})
  self.bags=after; self.posting=nil
end
function L:ReadDraft()
  if not GetSendMailItemLink or not GetSendMailItem then return end
  local lines={}
  for i=1,(ATTACHMENTS_MAX_SEND or 12) do
    local quantity=select(4,GetSendMailItem(i))
    local line=self:Line(GetSendMailItemLink(i),quantity)
    if line then
      local key=self:Key(line)
      if lines[key] then lines[key].quantity=lines[key].quantity+quantity else lines[key]=line end
    end
  end
  local items={}; for _,line in pairs(lines) do items[#items+1]=line end
  self.draft={items=items,money=GetSendMailMoney and GetSendMailMoney() or 0,
    postage=GetSendMailPrice and GetSendMailPrice()}
end
function L:SendIntent(recipient)
  if C.Production and C.Production.db and C.Production:MailIntent(recipient,self.draft) then return end
  if not self.draft or #self.draft.items==0 and self.draft.money==0 then return end
  local target
  for key in pairs(C.db.accounting_characters or {}) do
    local server,name=key:match("^(.-):(.+)$")
    local full=name and name .. "-" .. server:gsub("%s","")
    if server==GetRealmName() and name~=UnitName("player")
      and (recipient:lower()==name:lower() or recipient:lower()==full:lower()) then target=name end
  end
  self.sending={draft=C:Clone(self.draft),target=target,before=C:Clone(self.bags or self:Snapshot())}
end
function L:SentMail()
  if C.Production and C.Production.db and C.Production:MailSent() then return end
  local p=self.sending; if not p then return end; self.sending=nil
  if not p.target then self:Issue("Отправлены предметы: получатель не подтверждён как собственный персонаж",{items=p.draft.items}); return end
  local after=self:Snapshot(); if not after or not p.before then return end
  local expected={}; for _,line in ipairs(p.draft.items) do expected[self:Key(line)]=-line.quantity end
  local changes=self:Delta(p.before,after); local valid=true
  for _,change in ipairs(changes) do
    local key=self:Key(change.line)
    if expected[key]~=change.delta then valid=false end
    expected[key]=nil
  end
  if next(expected) or not valid then self:Issue("Состав отправленной почты требует сверки",{items=p.draft.items}); return end
  if #p.draft.items>0 then
    local saved=self:Emit("transfer",{source="own_mail",inputs=p.draft.items,outputs=p.draft.items,target_character=p.target,cash_delta_copper=0})
    if saved then
      self.db.transfer_receivable=self.db.transfer_receivable or {}
      for _,line in ipairs(p.draft.items) do
        self.db.transfer_receivable[#self.db.transfer_receivable+1]={target=p.target,sender=UnitName("player"),realm=GetRealmName(),line=C:Clone(line)}
      end
    end
  else self:Emit("receipt",{source="own_mail",cash_delta_copper=0,reason="Денежный перевод между собственными персонажами не является прибылью"}) end
  local paid=p.before.money-after.money-p.draft.money
  if C:Integer(p.draft.postage,true) and paid==p.draft.postage and paid>0 then
    self:Emit("expense",{source="postage",money_copper=paid})
  end
  self.bags=after
end
function L:InitializeCapture()
  hook(_G,"BuyMerchantItem",function() L:VendorIntent() end)
  hook(C_Container and C_Container.UseContainerItem and C_Container or _G,"UseContainerItem",function() if L.merchant and not L.transform then L:VendorIntent() end end)
  hook(_G,"SellCursorItem",function() L:VendorIntent() end)
  hook(_G,"DoTradeSkill",function(index) L:RecipeIntent(nil,index) end)
  hook(C_TradeSkillUI,"CraftRecipe",function(recipe) L:RecipeIntent(recipe) end)
  hook(_G,"SendMail",function(recipient) L:SendIntent(recipient) end)
  for _,entry in ipairs({{"TakeInboxMoney","money"},{"TakeInboxItem","item"},{"AutoLootMailItem","all"}}) do
    local mode=entry[2]; hook(_G,entry[1],function(index) L:MailIntent(index,mode) end)
  end
  hook(C_AuctionHouse,"PostItem",function(location,duration,quantity,_,buyout) L:PostIntent(location,duration,quantity,buyout,false) end)
  hook(C_AuctionHouse,"PostCommodity",function(location,duration,quantity,unit) L:PostIntent(location,duration,quantity,unit,true) end)
  hook(C_AuctionHouse,"ConfirmPostItem",function(location,duration,quantity,_,buyout) L:PostIntent(location,duration,quantity,buyout,false) end)
  hook(C_AuctionHouse,"ConfirmPostCommodity",function(location,duration,quantity,unit) L:PostIntent(location,duration,quantity,unit,true) end)
  hook(C_AuctionHouse,"StartCommoditiesPurchase",function(item,quantity)
    if not C.db.pending then L.externalBuy={item=item,quantity=quantity,started=GetTime()} end
  end)
  hook(C_AuctionHouse,"ConfirmCommoditiesPurchase",function(item,quantity)
    if L.externalBuy and L.externalBuy.item==item and L.externalBuy.quantity==quantity then L.externalBuy.confirmed=true end
  end)
  hook(C_AuctionHouse,"PlaceBid",function(auction,cost)
    if C.db.pending then return end
    local row=C_AuctionHouse.GetAuctionInfoByID and C_AuctionHouse.GetAuctionInfoByID(auction)
    if row and row.buyoutAmount==cost and row.itemKey then
      L.externalItem={auction=auction,cost=cost,item=row.itemLink or row.itemKey.itemID,key=row.itemKey,quantity=1,started=GetTime()}
    end
  end)
end
function L:InitializePublicCapture()
  hook(_G,"DoTradeSkill",function(index) L:RecipeIntent(nil,index) end)
  hook(C_TradeSkillUI,"CraftRecipe",function(recipe) L:RecipeIntent(recipe) end)
end
local frame=CreateFrame("Frame")
for _,event in ipairs({"PLAYER_ENTERING_WORLD","BAG_UPDATE_DELAYED","MERCHANT_SHOW","MERCHANT_UPDATE","MERCHANT_CLOSED",
  "UNIT_SPELLCAST_SENT","UNIT_SPELLCAST_SUCCEEDED","UNIT_SPELLCAST_INTERRUPTED","UNIT_SPELLCAST_FAILED","LOOT_OPENED","LOOT_CLOSED",
  "MAIL_SHOW","MAIL_INBOX_UPDATE","MAIL_FAILED","MAIL_CLOSED","MAIL_SEND_INFO_UPDATE","MAIL_SEND_SUCCESS","MAIL_SEND_FAILED","AUCTION_HOUSE_AUCTION_CREATED","OWNED_AUCTIONS_UPDATED","AUCTION_CANCELED","AUCTION_HOUSE_AUCTIONS_EXPIRED",
  "COMMODITY_PRICE_UPDATED","COMMODITY_PURCHASE_SUCCEEDED","COMMODITY_PURCHASE_FAILED","ITEM_PURCHASED"}) do
  pcall(frame.RegisterEvent,frame,event)
end
frame:SetScript("OnEvent",function(_,event,...)
  if not L.db then return end
  if C.collectorOnly and event~="BAG_UPDATE_DELAYED" and event~="UNIT_SPELLCAST_SENT"
    and event~="UNIT_SPELLCAST_SUCCEEDED" and event~="UNIT_SPELLCAST_INTERRUPTED"
    and event~="UNIT_SPELLCAST_FAILED" and event~="LOOT_OPENED" and event~="LOOT_CLOSED" then return end
  local a,b,c=...
  if event=="PLAYER_ENTERING_WORLD" then L:Opening()
  elseif event=="MERCHANT_SHOW" then L:MerchantSnapshot()
  elseif event=="MERCHANT_UPDATE" then if not L.vendorBefore then L:MerchantSnapshot() end
  elseif event=="MERCHANT_CLOSED" then
    if L.vendorBefore then L.merchantClosing=true; L:VendorFinish() else L.merchant=nil end
  elseif event=="BAG_UPDATE_DELAYED" then
    if L.transform then L:ScheduleTransform() elseif not L.vendorBefore then L.bags=L:Snapshot() end
  elseif event=="UNIT_SPELLCAST_SENT" and a=="player" then
    local spell=select(4,...)
    if spell==13262 then
      if L.transform then L:TransformFinish() end
      L.transform={kind="disenchant",recipe=spell,before=L:Snapshot(),started=GetTime(),success=0}
      L.db.interrupted={kind="disenchant"}
    end
  elseif event=="UNIT_SPELLCAST_SUCCEEDED" and a=="player" and L.transform then
    if not L.transform.recipe or L.transform.recipe==c then
      L.transform.success=L.transform.success+1; L.transform.lastSuccess=GetTime()
      L.db.interrupted={kind=L.transform.kind,recipe_id=L.transform.recipe}; L:ScheduleTransform()
    end
  elseif (event=="UNIT_SPELLCAST_INTERRUPTED" or event=="UNIT_SPELLCAST_FAILED") and a=="player" then
    if L.transform and (not L.transform.recipe or L.transform.recipe==c) then L:TransformFinish(); L.transform=nil; L.db.interrupted=nil end
  elseif event=="LOOT_OPENED" and L.transform and L.transform.kind=="disenchant" then
    L.transform.allowedOutputs={}
    if GetNumLootItems and GetLootSlotLink then
      for i=1,GetNumLootItems() do local line=L:Line(GetLootSlotLink(i),1); if line then L.transform.allowedOutputs[line.item_id]=true end end
    end
  elseif event=="LOOT_CLOSED" and L.transform and L.transform.kind=="disenchant" then L.transform.lootClosed=true; L:ScheduleTransform()
  elseif event=="MAIL_SHOW" then L.inbox=L:ReadInbox(); L.mailPending={}
  elseif event=="MAIL_SEND_INFO_UPDATE" then L:ReadDraft()
  elseif event=="MAIL_SEND_SUCCESS" then C_Timer.After(.5,function() L:SentMail() end)
  elseif event=="MAIL_SEND_FAILED" then L.sending=nil; L:Issue("Отправка почты не подтверждена")
  elseif event=="MAIL_INBOX_UPDATE" then L:InboxUpdate()
  elseif event=="MAIL_FAILED" then L:Issue("Получение почты не подтверждено"); L.mailPending={}; L.db.mail_pending=nil
  elseif event=="MAIL_CLOSED" then
    for _,pending in pairs(L.mailPending or {}) do L:Issue("Почта закрыта до подтверждения операции",{mail=pending.mail}) end
    L.mailPending={}; L.db.mail_pending=nil; L.inbox=nil
  elseif event=="AUCTION_HOUSE_AUCTION_CREATED" then
    local id=a; C_Timer.After(.5,function() L:Posted(id) end)
  elseif event=="AUCTION_CANCELED" or event=="AUCTION_HOUSE_AUCTIONS_EXPIRED" then
    local listing=L.db.listings and L.db.listings[tostring(a)]
    if listing then listing.returned=true end
  elseif event=="COMMODITY_PRICE_UPDATED" and L.externalBuy then L.externalBuy.cost=b
  elseif event=="COMMODITY_PURCHASE_SUCCEEDED" and L.externalBuy then
    local p=L.externalBuy; L.externalBuy=nil
    if p.confirmed and GetTime()-p.started<60 and C:Integer(p.cost,true) then
      L:Purchase("external:" .. C:Now() .. ":" .. L.db.sequence,p.item,p.quantity,p.cost,{source="auction"})
    end
  elseif event=="COMMODITY_PURCHASE_FAILED" then L.externalBuy=nil
  elseif event=="ITEM_PURCHASED" and L.externalItem and (L.externalItem.auction==a or L.externalItem.key.itemID==a) then
    local p=L.externalItem; L.externalItem=nil
    if GetTime()-p.started<60 then L:Purchase("auction:" .. p.auction,p.item,p.quantity,p.cost,{source="auction",details={item_key=p.key}}) end
  end
end)
