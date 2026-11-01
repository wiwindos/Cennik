local _,C=...
local P={prefix="CENNIKP1"}
C.Production=P
local function same(a,b)
  local function clean(value) return (value or ""):lower():gsub("%s","") end
  return clean(a)==clean(b) or clean(a)==clean((b or ""):match("^([^-]+)"))
end
local function map(lines)
  local out={}; for _,line in ipairs(lines or {}) do local k=line.item_id .. ":" .. (line.variant or "");out[k]=(out[k] or 0)+line.quantity end
  return out
end
local function equal(a,b)
  local left,right=map(a),map(b)
  for k,q in pairs(left) do if right[k]~=q then return false end end
  for k,q in pairs(right) do if left[k]~=q then return false end end
  return true
end
local function compact(lines)
  local result,byKey={},{}
  for _,line in ipairs(lines or {}) do
    local key=line.item_id .. ":" .. (line.variant or "")
    if not byKey[key] then local row=C:Clone(line);row.quantity=0;byKey[key]=row;result[#result+1]=row end
    byKey[key].quantity=byKey[key].quantity+line.quantity
  end
  return result
end
function P:CanRecord()
  return self.db and #self.db.events<2000 and C.Accounting.db and #C.Accounting.db.events<4998
    and C:MarketBindingMatches(C.Accounting.market)
end
function P:Notice(text) self.message=text; if self.window and self.window:IsShown() then self:Render() end end
function P:Client(id)
  for _,client in ipairs(self.config.clients or {}) do if client.id==id then return client end end
end
function P:Order(id)
  for _,order in ipairs(self.config.orders or {}) do if order.order_id==id then return order end end
end
function P:Transfer(id)
  for _,order in ipairs(self.config.orders or {}) do for _,transfer in ipairs(order.transfers or {}) do if transfer.id==id then return transfer,order end end end
end
function P:Record(kind,fields)
  if not self.db or #self.db.events>=2000 then self:Notice("Очередь производства заполнена: /reload и синхронизация");return end
  self.db.sequence=self.db.sequence+1
  local event={event_id=self.config.client.id .. ":production:" .. self.db.sequence,
    workspace_id=self.config.workspace_id,client_id=self.config.client.id,observed_at=math.floor(C:Now()),kind=kind,lines={}}
  for k,v in pairs(fields) do event[k]=C:Clone(v) end
  self.db.events[#self.db.events+1]=event
  if #self.db.outbox<500 then self.db.outbox[#self.db.outbox+1]=event end
  return event
end
function P:Initialize()
  self.db,self.config=nil,nil
  local config=CennikImport and CennikImport.production
  if not config or config.format_version~=1 then return end
  if config.client.character~=UnitName("player") or config.client.realm~=GetRealmName()
    or config.workspace_id~=C.db.workspace_id or config.client.id~=C.db.client_id then return end
  self.config=config
  C.db.production=C.db.production or {events={},sequence=0,part_sequence=0,parcels={},sent={},received={},done={}}
  self.db=C.db.production;self.db.outbox={}
  self.db.sequence=math.max(self.db.sequence or 0,config.sequence or 0)
  self.db.part_sequence=math.max(self.db.part_sequence or 0,config.part_sequence or 0)
  local ack={};for _,id in ipairs(config.acknowledged_event_ids or {}) do ack[id]=true end
  local kept={};for _,e in ipairs(self.db.events) do if not ack[e.event_id] then kept[#kept+1]=e end end
  self.db.events=kept; for i=1,math.min(500,#kept) do self.db.outbox[i]=kept[i] end
  -- Manifest snapshots from web allow reception when peer messages were missed.
  for _,order in ipairs(config.orders or {}) do
    self.db.order_stock=self.db.order_stock or {}
    if not self.db.order_stock[order.order_id] then self.db.order_stock[order.order_id]=C:Clone(order.crafter_stock or {}) end
    for _,step in ipairs(order.steps or {}) do self.db.done[step.step_id]=math.max(self.db.done[step.step_id] or 0,step.done or 0) end
    for _,transfer in ipairs(order.transfers or {}) do
      local serverSent={}
      for _,part in ipairs(transfer.parts or {}) do
        if transfer.from_client==config.client.id then
          self.db.sent[transfer.id]=self.db.sent[transfer.id] or {}
          for key,q in pairs(part.sent or {}) do local item=key:match("^(%d+):");serverSent[item]=(serverSent[item] or 0)+q end
        end
        if transfer.to_client==config.client.id then
          local received=self.db.received[part.part_id] or {};self.db.received[part.part_id]=received
          for key,q in pairs(part.received or {}) do received[key]=math.max(received[key] or 0,q) end
          local lines={}
          for key,q in pairs(part.sent or {}) do
            local item,variant=key:match("^(%d+):(.*)$")
            lines[#lines+1]={item_id=tonumber(item),quantity=q,variant=variant}
          end
          if #lines>0 then self.db.parcels[part.part_id]=self.db.parcels[part.part_id] or {order_id=order.order_id,transfer_id=transfer.id,
            part_id=part.part_id,mode=part.mode,lines=lines,ready=true} end
        end
      end
      for item,q in pairs(serverSent) do self.db.sent[transfer.id][item]=math.max(self.db.sent[transfer.id][item] or 0,q) end
    end
  end
  if C_ChatInfo and C_ChatInfo.RegisterAddonMessagePrefix then pcall(C_ChatInfo.RegisterAddonMessagePrefix,self.prefix)
  elseif RegisterAddonMessagePrefix then pcall(RegisterAddonMessagePrefix,self.prefix) end
  self:CaptureRecipes()
  if config.client.role=="crafter" and C.Sniper then C.Sniper:HoldForManualSearch("Рабочее окно крафтера") end
end
function P:Send(text,recipient)
  if #text>240 then return end
  if C_ChatInfo and C_ChatInfo.SendAddonMessage then pcall(C_ChatInfo.SendAddonMessage,self.prefix,text,"WHISPER",recipient)
  elseif SendAddonMessage then pcall(SendAddonMessage,self.prefix,text,"WHISPER",recipient) end
end
function P:Manifest(part)
  local transfer=self:Transfer(part.transfer_id); if not transfer then return end
  local peer=self:Client(transfer.to_client); if not peer then return end
  local recipient=peer.character .. "-" .. peer.realm:gsub("%s","")
  local prefix=table.concat({"1",self.config.workspace_id,self.config.client.id,part.order_id,part.transfer_id,part.part_id,part.mode},"|")
  self:Send(prefix .. "|H|" .. #part.lines,recipient)
  for i,line in ipairs(part.lines) do self:Send(prefix .. "|L|" .. i .. "|" .. line.item_id .. "|" .. line.quantity .. "|" .. (line.variant or ""),recipient) end
end
function P:OnMessage(prefix,text,sender)
  if not self.db or prefix~=self.prefix or type(text)~="string" or #text>240 then return end
  local f={};for field in (text .. "|"):gmatch("([^|]*)|") do f[#f+1]=field end
  if f[1]~="1" or f[2]~=self.config.workspace_id then return end
  local transfer,order=self:Transfer(f[5]); local peer=self:Client(f[3])
  if not transfer or not peer or not same(sender,peer.character .. "-" .. peer.realm:gsub("%s",""))
    or transfer.from_client~=peer.id or transfer.to_client~=self.config.client.id or order.order_id~=f[4]
    or not f[6] or not f[6]:match("^" .. peer.id .. ":%d+$") or (f[7]~="mail" and f[7]~="trade") then return end
  local parcel=self.db.parcels[f[6]] or {order_id=f[4],transfer_id=f[5],part_id=f[6],mode=f[7],lines={}}
  if parcel.ready then return end
  if f[8]=="H" then
    local count=tonumber(f[9]);if not C:Integer(count) or count>100 then return end;parcel.count=count
  elseif f[8]=="L" then
    local i,item,q=tonumber(f[9]),tonumber(f[10]),tonumber(f[11])
    if not C:Integer(i) or i>100 or not C:Integer(item) or not C:Integer(q) or q>1000000
      or not (f[12] or ""):match("^[0-9:.-]*$") then return end
    local allowed=false
    for _,line in ipairs(transfer.lines or {}) do if line.item_id==item then allowed=true end end
    for _,id in ipairs(transfer.allowed_items or {}) do if id==item then allowed=true end end
    if not allowed then return end
    parcel.lines[i]={item_id=item,quantity=q,variant=f[12] or ""}
  else return end
  self.db.parcels[f[6]]=parcel
  local n=0;for _ in pairs(parcel.lines) do n=n+1 end
  if parcel.count and n==parcel.count then
    for i=1,parcel.count do if not parcel.lines[i] then return end end
    parcel.ready=true
    if parcel.mode=="trade" and self.tradeBefore and same(self.tradePartner,peer.character) then parcel.tradeToken=self.tradeToken end
    self:Notice("Получен манифест передачи от " .. peer.character)
  end
end
function P:CaptureRecipes()
  if not self.db then return end
  local report=self.db.recipes or {recipes={},observed_at=math.floor(C:Now()),coverage="visible_recipes_only"}
  if GetNumTradeSkills and GetTradeSkillRecipeLink and GetTradeSkillItemLink and GetTradeSkillNumReagents and GetTradeSkillReagentInfo and GetTradeSkillReagentItemLink then
    for index=1,(GetNumTradeSkills() or 0) do
      local recipeLink=GetTradeSkillRecipeLink(index)
      local spell=recipeLink and tonumber(recipeLink:match("enchant:(%d+)") or recipeLink:match("spell:(%d+)"))
      local output=C.Accounting:Line(GetTradeSkillItemLink(index),1)
      if spell and output then
        local inputs={};for i=1,(GetTradeSkillNumReagents(index) or 0) do
          local _,_,q=GetTradeSkillReagentInfo(index,i);local line=C.Accounting:Line(GetTradeSkillReagentItemLink(index,i),q)
          if line then inputs[#inputs+1]=line end
        end
        local qmin,qmax
        if GetTradeSkillNumMade then qmin,qmax=GetTradeSkillNumMade(index) end
        report.recipes[tostring(spell)]={spell_id=spell,item_id=output.item_id,name=output.item_name,inputs=inputs,qty_min=qmin,qty_max=qmax}
      end
    end
  elseif C_TradeSkillUI and C_TradeSkillUI.GetAllRecipeIDs and C_TradeSkillUI.GetRecipeInfo then
    for _,id in ipairs(C_TradeSkillUI.GetAllRecipeIDs() or {}) do
      local info=C_TradeSkillUI.GetRecipeInfo(id)
      if info and info.learned then report.recipes[tostring(id)]=C:ObservedModernRecipe(id,info.name) end
    end
  end
  if IsPlayerSpell then report.disenchant=not not IsPlayerSpell(13262) elseif IsSpellKnown then report.disenchant=not not IsSpellKnown(13262) end
  report.observed_at=math.floor(C:Now());self.db.recipes=report
end
function P:Selected()
  local orders=self.config and self.config.orders or {}
  return orders[self.orderIndex or 1]
end
function P:ReadyLines(transfer)
  local snapshot=C.Accounting:Snapshot();if not snapshot then return {} end
  local limits={}
  if transfer.direction=="materials" then
    for _,line in ipairs(transfer.lines) do limits[line.item_id]=line.quantity end
  else
    -- Only this order's received inputs and observed outputs, less consumption
    -- and already sent returns. Other orders' stock is not offered.
    for _,event in ipairs(self.db.events) do
      if event.order_id==transfer.order_id and event.kind=="received" then
        for _,line in ipairs(event.lines) do limits[line.item_id]=(limits[line.item_id] or 0)+line.quantity end
      end
    end
    local stock=self.db.order_stock and self.db.order_stock[transfer.order_id] or {}
    for item,q in pairs(stock) do limits[tonumber(item)]=q end
  end
  local sent=self.db.sent[transfer.id] or {}
  local lines={}
  for _,line in pairs(snapshot.items) do
    local left=(limits[line.item_id] or 0)-(sent[tostring(line.item_id)] or 0)
    local take=math.min(line.quantity,math.max(0,left))
    if take>0 then local row=C:Clone(line);row.quantity=take;lines[#lines+1]=row;limits[line.item_id]=limits[line.item_id]-take end
  end
  table.sort(lines,function(a,b)return a.item_id<b.item_id end)
  return lines
end
function P:Prepare(mode)
  if not self.db then return false end
  if not self:CanRecord() then self:Notice("Сначала сохраните очереди: /reload и синхронизация");return false end
  if self.active then self:Notice("Завершите или отмените текущую подготовку");return false end
  local order=self:Selected();if not order then self:Notice("Сначала создайте заказ в вебе и выполните /reload");return false end
  local transfer
  for _,value in ipairs(order.transfers) do if value.from_client==self.config.client.id then transfer=value end end
  local lines=transfer and self:ReadyLines(transfer) or {}
  if #lines==0 then self:Notice("Нет доступных предметов этого заказа для передачи");return false end
  self.db.part_sequence=self.db.part_sequence+1
  self.active={order_id=order.order_id,transfer_id=transfer.id,part_id=self.config.client.id .. ":" .. self.db.part_sequence,
    mode=mode,lines=lines,before=C.Accounting:Snapshot()}
  local peer=self:Client(transfer.to_client)
  local frame=mode=="mail" and SendMailFrame or TradeFrame
  if not frame or not frame:IsShown() then self.active=nil;self:Notice(mode=="mail" and "Откройте отправку почты" or "Откройте обмен с " .. peer.character);return false end
  if mode=="trade" and not same(UnitName("NPC"),peer.character) then
    self.active=nil;self:Notice("Окно обмена открыто с другим персонажем");return false
  end
  local hasItems=mode=="mail" and GetSendMailItemLink or GetTradePlayerItemLink
  if hasItems then for i=1,(mode=="mail" and 12 or 6) do if hasItems(i) then self.active=nil;self:Notice("Сначала освободите вложения/предметы текущего окна");return false end end end
  if mode=="mail" then
    if SendMailNameEditBox then SendMailNameEditBox:SetText(peer.character .. "-" .. peer.realm:gsub("%s","")) end
    if SendMailSubjectEditBox then SendMailSubjectEditBox:SetText("Cennik " .. transfer.id .. " " .. self.db.part_sequence) end
  end
  self:Fill(mode,lines)
  self:Notice("Подготовка для " .. peer.character .. ". Проверьте состав и подтвердите " .. (mode=="mail" and "отправку" or "обмен") .. " в игре")
  return true
end
function P:Fill(mode,lines)
  local snapshot=C.Accounting:Snapshot();local api=C_Container or _G
  local pickup=api.PickupContainerItem or PickupContainerItem;local split=api.SplitContainerItem or SplitContainerItem
  local put=mode=="mail" and ClickSendMailItemButton or ClickTradeButton
  local capacity=mode=="mail" and (ATTACHMENTS_MAX_SEND or 12) or (MAX_TRADE_ITEMS and MAX_TRADE_ITEMS-1 or 6)
  if not snapshot or not pickup or not put then return end
  local slot=1
  for _,line in ipairs(lines) do
    local left=line.quantity
    for key,item in pairs(snapshot.slots) do
      if left>0 and slot<=capacity and C.Accounting:Key(item)==C.Accounting:Key(line) then
        local bag,index=key:match("^(%-?%d+):(%d+)$");local take=math.min(left,item.quantity)
        if GetCursorInfo and GetCursorInfo() then return end
        if take<item.quantity then if not split then return end;split(tonumber(bag),tonumber(index),take) else pickup(tonumber(bag),tonumber(index)) end
        put(slot);slot=slot+1;left=left-take
      end
    end
  end
end
function P:MailIntent(recipient,draft)
  local part=self.active;if not part or part.mode~="mail" then return false end
  local transfer=self:Transfer(part.transfer_id);local peer=self:Client(transfer.to_client)
  local maximum=map(part.lines)
  if not same(recipient,peer.character .. "-" .. peer.realm:gsub("%s","")) or not draft or #draft.items==0 or draft.money~=0 then
    self:Notice("Неверный получатель или состав письма");part.invalid=true;return true
  end
  for key,q in pairs(map(draft.items)) do if q>(maximum[key] or 0) then part.invalid=true end end
  if not part.invalid then
    part.lines=compact(draft.items);part.intent=true;part.postage=GetSendMailPrice and GetSendMailPrice() or 0
    self:Manifest(part)
  end
  return true
end
function P:Prove(part,kind,lines,before,after)
  local changes=C.Accounting:Delta(before,after);local actual={}
  for _,change in ipairs(changes) do
    if (kind=="sent" and change.delta>0) or (kind=="received" and change.delta<0) then return false end
    local row=C:Clone(change.line);row.quantity=math.abs(change.delta);actual[#actual+1]=row
  end
  return equal(actual,lines)
end
function P:CommitSent(part)
  if self.db.sent_parts and self.db.sent_parts[part.part_id] then return end
  if not self:CanRecord() then self:Notice("Отправка требует сверки: очередь заполнена");return end
  local transfer=self:Transfer(part.transfer_id);local peer=self:Client(transfer.to_client)
  local order=self:Order(part.order_id)
  local ledger=C.Accounting:Emit("transfer",{source="production_" .. part.mode,inputs=part.lines,outputs=part.lines,
    target_character=peer.character,cash_delta_copper=0,details={production_order_id=part.order_id,production_part_id=part.part_id,
      production_task_ids=order and order.task_ids or {}}})
  if not ledger then return end
  if part.mode=="mail" and part.postage and part.postage>0 then
    local after=C.Accounting:Snapshot()
    if after and part.before.money-after.money==part.postage then
      C.Accounting:Emit("expense",{source="production_mail",money_copper=part.postage,
        reason="Почтовые расходы производственного заказа",details={production_order_id=part.order_id}})
    else C.Accounting:Issue("Не подтверждены почтовые расходы заказа",{production_order_id=part.order_id}) end
  end
  self:Record("sent",{order_id=part.order_id,transfer_id=part.transfer_id,part_id=part.part_id,mode=part.mode,lines=part.lines})
  self.db.sent_parts=self.db.sent_parts or {};self.db.sent_parts[part.part_id]=true
  self.db.sent[transfer.id]=self.db.sent[transfer.id] or {}
  for _,line in ipairs(part.lines) do
    local key=tostring(line.item_id);self.db.sent[transfer.id][key]=(self.db.sent[transfer.id][key] or 0)+line.quantity
  end
  self.active=nil;self:Notice("Отправлено. Ожидается подтверждение получателя")
end
function P:MailSent()
  local part=self.active;if not part or part.mode~="mail" then return false end
  local after=C.Accounting:Snapshot()
  if part.intent and not part.invalid and after and self:Prove(part,"sent",part.lines,part.before,after) then self:CommitSent(part)
  else self:Record("review",{order_id=part.order_id,reason="Отправка письма требует сверки состава"});self.active=nil end
  return true
end
function P:Receive(part,lines)
  lines=compact(lines)
  if #lines==0 or not self:CanRecord() then return false end
  self.db.received[part.part_id]=self.db.received[part.part_id] or {}
  local received=self.db.received[part.part_id];local maximum=map(part.lines)
  for key,q in pairs(map(lines)) do if (received[key] or 0)+q>(maximum[key] or 0) then return false end end
  local ledger=C.Accounting:Emit("receipt",{source="production_" .. part.mode,outputs=lines,
    reason="Получена производственная передача; себестоимость перенесена отправителем",details={production_order_id=part.order_id,production_part_id=part.part_id}})
  if not ledger then return false end
  self:Record("received",{order_id=part.order_id,transfer_id=part.transfer_id,part_id=part.part_id,mode=part.mode,lines=lines})
  self.db.order_stock=self.db.order_stock or {};self.db.order_stock[part.order_id]=self.db.order_stock[part.order_id] or {}
  for _,line in ipairs(lines) do
    local key=C.Accounting:Key(line);received[key]=(received[key] or 0)+line.quantity
    local item=tostring(line.item_id);self.db.order_stock[part.order_id][item]=(self.db.order_stock[part.order_id][item] or 0)+line.quantity
  end
  self:Notice("Предметы заказа получены")
  return true
end
function P:MailReceived(mail)
  if not self.db then return false end
  local transferID,sequence=(mail.subject or ""):match("^Cennik ([a-f0-9]+) (%d+)$")
  if not transferID then return false end
  if not mail.items or #mail.items==0 then return true end
  local transfer,order=self:Transfer(transferID)
  if not transfer or transfer.to_client~=self.config.client.id then return false end
  local peer=self:Client(transfer.from_client)
  local part=self.db.parcels[peer.id .. ":" .. sequence]
  if not same(mail.sender,peer.character .. "-" .. peer.realm:gsub("%s","")) or not part or not part.ready or not self:Receive(part,mail.items) then
    self:Record("review",{order_id=order.order_id,reason="Получение письма требует манифеста/сверки"})
  end
  return true
end
function P:TradeComplete()
  if not self.tradeBefore or not self.tradeAccepted or self.tradeCompleting then return end
  self.tradeCompleting=true
  local before,partner=self.tradeBefore,self.tradePartner
  local token=self.tradeToken
  local active=self.active;local sender=active~=nil and active.mode=="trade"
  local attempts=0
  local function verify()
    attempts=attempts+1
    local part=sender and active or nil
    local after=C.Accounting:Snapshot()
    if not sender and after then
      for _,candidate in pairs(P.db.parcels) do
        local transfer=P:Transfer(candidate.transfer_id);local peer=transfer and P:Client(transfer.from_client)
        local received=P.db.received[candidate.part_id]
        if candidate.mode=="trade" and candidate.tradeToken==token and candidate.ready and peer and same(partner,peer.character)
          and (not received or not next(received)) and P:Prove(candidate,"received",candidate.lines,before,after)
          and (not part or tonumber(candidate.part_id:match(":(%d+)$"))>tonumber(part.part_id:match(":(%d+)$"))) then part=candidate end
      end
    end
    local transfer=part and P:Transfer(part.transfer_id)
    local peer=transfer and P:Client(transfer[sender and "to_client" or "from_client"])
    if part and peer and same(partner,peer.character) and after and P:Prove(part,sender and "sent" or "received",part.lines,before,after) then
      if sender then P:CommitSent(part) else P:Receive(part,part.lines) end
    elseif attempts<8 then C_Timer.After(.25,verify);return
    elseif part then P:Record("review",{order_id=part.order_id,reason="Обмен завершён, но состав не совпал с манифестом"})
    else P:Notice("Обмен завершён без подходящего манифеста; нужна сверка") end
    P.tradeBefore,P.tradeAccepted,P.tradeCompleting=nil,nil,nil
  end
  C_Timer.After(.5,verify)
end
function P:Enrich(kind,fields)
  if not self.db or (kind~="craft" and kind~="disenchant") then return end
  local active=self.db.active_step
  if not active then return end
  local order=self:Order(active.order_id);if not order then return end
  local step;for _,s in ipairs(order.steps) do if s.step_id==active.step_id then step=s end end
  if not step or step.kind~=kind or step.spell_id~=(fields.details and fields.details.recipe_id) then return end
  local count=kind=="disenchant" and 1 or fields.details.successful_casts or 0
  local actual,expected={},{}
  for _,line in ipairs(fields.inputs or {}) do actual[line.item_id]=(actual[line.item_id] or 0)+line.quantity end
  for _,line in ipairs(step.inputs) do expected[line.item_id]=line.quantity*count end
  local valid=count>0 and count<=step.crafts-(self.db.done[step.step_id] or 0)
  for item,q in pairs(actual) do if expected[item]~=q then valid=false end end
  for item,q in pairs(expected) do if actual[item]~=q then valid=false end end
  if not valid then
    self.db.active_step=nil
    self:Record("review",{order_id=order.order_id,reason="Изготовление/распыление не соответствует выбранному этапу; учтено отдельно"})
    return
  end
  fields.task_id,fields.strategy_id=step.task_id,step.strategy_id
  fields.details.production_order_id=order.order_id;fields.details.production_step_id=step.step_id
end
function P:Observe(event)
  if not self.db or not event.details or not event.details.production_step_id then return end
  local order=self:Order(event.details.production_order_id);if not order then return end
  local count=event.kind=="disenchant" and 1 or event.details.successful_casts or 0
  if count<=0 then return end
  self:Record("crafted",{order_id=order.order_id,step_id=event.details.production_step_id,crafts=count,lines=event.outputs,inputs=event.inputs})
  self.db.done[event.details.production_step_id]=(self.db.done[event.details.production_step_id] or 0)+count
  for _,step in ipairs(order.steps) do
    if step.step_id==event.details.production_step_id and self.db.done[step.step_id]>=step.crafts then self.db.active_step=nil end
  end
  self.db.order_stock=self.db.order_stock or {};local stock=self.db.order_stock[order.order_id] or {};self.db.order_stock[order.order_id]=stock
  for _,line in ipairs(event.inputs) do local key=tostring(line.item_id);stock[key]=(stock[key] or 0)-line.quantity end
  for _,line in ipairs(event.outputs) do local key=tostring(line.item_id);stock[key]=(stock[key] or 0)+line.quantity end
  self:Notice("Этап обновлён по фактическому результату")
end
function P:Available(step)
  local snapshot=C.Accounting:Snapshot();if not snapshot then return 0 end
  local count=math.max(0,step.crafts-(self.db.done[step.step_id] or 0))
  local order=self:Selected();local stock=self.db.order_stock and self.db.order_stock[order.order_id] or {}
  local returned={}
  for _,transfer in ipairs(order.transfers) do
    if transfer.direction=="results" then returned=self.db.sent[transfer.id] or {} end
  end
  for _,input in ipairs(step.inputs) do
    local owned=0;for _,line in pairs(snapshot.items) do if line.item_id==input.item_id then owned=owned+line.quantity end end
    local available=(stock[tostring(input.item_id)] or 0)-(returned[tostring(input.item_id)] or 0)
    count=math.min(count,math.floor(math.min(owned,available)/input.quantity))
  end
  return math.max(0,count)
end
function P:Craft()
  local order=self:Selected();local step=order and order.steps[self.stepIndex or 1]
  if not step or order.crafter_id~=self.config.client.id then return end
  if not self:CanRecord() then self:Notice("Сначала сохраните очереди: /reload и синхронизация");return end
  local count=self:Available(step)
  if count<=0 then self:Notice("Недостаточно полученных материалов этого заказа");return end
  local function activate() P.db.active_step={order_id=order.order_id,step_id=step.step_id} end
  if step.kind=="disenchant" then
    if self.db.recipes and self.db.recipes.disenchant and CastSpellByID then activate();CastSpellByID(13262);self:Notice("Выберите исходный предмет в сумке") else self:Notice("Распыление не подтверждено у персонажа") end
    return
  end
  local index
  if GetNumTradeSkills and GetTradeSkillRecipeLink then
    for i=1,(GetNumTradeSkills() or 0) do local link=GetTradeSkillRecipeLink(i);if link and (tonumber(link:match("enchant:(%d+)") or link:match("spell:(%d+)")))==step.spell_id then index=i;break end end
  end
  if index and DoTradeSkill then activate();if SelectTradeSkill then SelectTradeSkill(index) end;DoTradeSkill(index,count)
  elseif C_TradeSkillUI and C_TradeSkillUI.GetRecipeInfo and C_TradeSkillUI.CraftRecipe then
    local info=C_TradeSkillUI.GetRecipeInfo(step.spell_id)
    if info and info.learned then activate();C_TradeSkillUI.CraftRecipe(step.spell_id,count) else self:Notice("Откройте профессию и проверьте изучение рецепта") end
  else self:Notice("Откройте нужную профессию, сбросьте фильтры и нажмите снова") end
end
function P:Show()
  if not self.db then print("Cennik: настройте два аккаунта и создайте заказ в вебе");return end
  if not self.window then
    local w=CreateFrame("Frame","CennikProductionWindow",UIParent,"BasicFrameTemplateWithInset");self.window=w
    w:SetSize(670,490);w:SetPoint("CENTER");w:SetFrameStrata("DIALOG")
    self.text=w:CreateFontString(nil,"OVERLAY","GameFontHighlight");self.text:SetPoint("TOPLEFT",18,-40);self.text:SetSize(630,300);self.text:SetJustifyH("LEFT");self.text:SetJustifyV("TOP")
    local function button(label,x,y,width,fn)
      local b=CreateFrame("Button",nil,w,"UIPanelButtonTemplate");b:SetPoint("TOPLEFT",x,y);b:SetSize(width,28);b:SetText(label);b:SetScript("OnClick",fn)
    end
    button("Следующий заказ",18,-350,190,function() P.orderIndex=(P.orderIndex or 1)%math.max(1,#P.config.orders)+1;P.stepIndex=1;P:Render() end)
    button("Следующий этап",220,-350,170,function() local o=P:Selected();P.stepIndex=(P.stepIndex or 1)%math.max(1,o and #o.steps or 1)+1;P:Render() end)
    button("Изготовить доступное",405,-350,235,function() P:Craft() end)
    button("Подготовить письмо",18,-390,190,function() P:Prepare("mail") end)
    button("Подготовить обмен",220,-390,190,function() P:Prepare("trade") end)
    button("Отменить подготовку",425,-390,215,function() P.active=nil;P:Notice("Подготовка отменена; предметы в письме/обмене верните вручную") end)
  end
  self.window:Show();self:Render()
end
function P:Render()
  if not self.text then return end
  local order=self:Selected();local lines={"Производство · " .. self.config.client.character .. " · " .. (self.config.client.role=="crafter" and "Крафтер" or "Аукционер")}
  if order then
    lines[#lines+1]="Заказ " .. order.order_id:sub(1,8)
    local selected=self.stepIndex or 1
    lines[#lines+1]="Этап " .. selected .. "/" .. #order.steps
    for i=math.max(1,selected-2),math.min(#order.steps,selected+2) do
      local step=order.steps[i]
      lines[#lines+1]=(i==(self.stepIndex or 1) and "> " or "  ") .. (step.kind=="disenchant" and "Распылить " or "Изготовить ") .. step.item_name ..
        "; выполнено " .. (self.db.done[step.step_id] or 0) .. "/" .. step.crafts .. "; доступно " .. self:Available(step)
    end
    local step=order.steps[selected]
    if step then
      local inputs={};for _,line in ipairs(step.inputs) do inputs[#inputs+1]="#" .. line.item_id .. " × " .. line.quantity end
      lines[#lines+1]="Материалы на действие: " .. table.concat(inputs,", ")
    end
  else lines[#lines+1]="Ожидаются адресные заказы из веба. /reload в обоих окнах после синхронизации." end
  lines[#lines+1]="\nПередача: откройте почту или обмен с назначенным персонажем.\nПодготовка заполняет предметы; отправку/обмен подтверждаете в игре."
  lines[#lines+1]=self.message or ""
  self.text:SetText(table.concat(lines,"\n"))
end
local frame=CreateFrame("Frame")
for _,event in ipairs({"CHAT_MSG_ADDON","TRADE_SKILL_SHOW","TRADE_SKILL_LIST_UPDATE","TRADE_SHOW","TRADE_ACCEPT_UPDATE","TRADE_CLOSED","TRADE_COMPLETE","UI_INFO_MESSAGE"}) do pcall(frame.RegisterEvent,frame,event) end
frame:SetScript("OnEvent",function(_,event,a,b,c,d)
  if not P.db then return end
  if event=="CHAT_MSG_ADDON" then P:OnMessage(a,b,d)
  elseif event=="TRADE_SKILL_SHOW" or event=="TRADE_SKILL_LIST_UPDATE" then P:CaptureRecipes()
  elseif event=="TRADE_SHOW" then
    P.tradeToken=(P.tradeToken or 0)+1;P.tradeBefore=C.Accounting:Snapshot();P.tradePartner=UnitName("NPC");P.tradeAccepted=false
  elseif event=="TRADE_ACCEPT_UPDATE" then
    local playerMoney=GetPlayerTradeMoney and GetPlayerTradeMoney() or 0
    local targetMoney=GetTargetTradeMoney and GetTargetTradeMoney() or 0
    P.tradeAccepted=a==1 and b==1 and playerMoney==0 and targetMoney==0
    if P.tradeAccepted and P.active and P.active.mode=="trade" then
      local part=P.active
      if GetTradePlayerItemInfo and GetTradePlayerItemLink then
        local lines={}
        for i=1,6 do
          local _,_,q=GetTradePlayerItemInfo(i);local line=C.Accounting:Line(GetTradePlayerItemLink(i),q)
          if line then lines[#lines+1]=line end
        end
        local maximum=map(part.lines)
        for key,q in pairs(map(lines)) do if q>(maximum[key] or 0) then P.tradeAccepted=false end end
        if #lines==0 then P.tradeAccepted=false end
        if P.tradeAccepted then part.lines=compact(lines) end
      end
      if P.tradeAccepted then
        if part.announced then P.db.part_sequence=P.db.part_sequence+1;part.part_id=P.config.client.id .. ":" .. P.db.part_sequence end
        part.announced=true;P:Manifest(part)
      end
    end
  elseif event=="TRADE_COMPLETE" or event=="UI_INFO_MESSAGE" and ERR_TRADE_COMPLETE and (a==ERR_TRADE_COMPLETE or b==ERR_TRADE_COMPLETE) then P:TradeComplete()
  elseif event=="TRADE_CLOSED" then P:Notice("Окно обмена закрыто; результат учитывается только после подтверждения завершения") end
end)
