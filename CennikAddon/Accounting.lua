local _, C = ...
local L={}
C.Accounting=L
local function character() return UnitName("player") end
local function realm() return GetRealmName() end
function L:Line(link,quantity)
  local item=type(link)=="number" and link or type(link)=="string" and tonumber(link:match("item:(%d+)"))
  if not C:Integer(item) or not C:Integer(quantity) then return nil end
  local info=C_Item and C_Item.GetItemInfo or GetItemInfo
  local name,level
  if info then local n,_,_,l=info(link or item); name,level=n,l end
  local variant=""
  if C.Adapter:Equipment(item) then
    local text=type(link)=="string" and link:match("item:([^|]+)") or ""
    local fields={}; for value in (text .. ":"):gmatch("([^:]*):") do fields[#fields+1]=value end
    variant=table.concat({level or 0,fields[7] or "0"},":")
  end
  return {item_id=item,quantity=quantity,variant=variant,item_name=name or C.Items:Name(item),link=type(link)=="string" and link or ""}
end
function L:Key(line) return line.item_id .. ":" .. (line.variant or "") end
function L:Snapshot()
  local api=C_Container
  local slots=api and api.GetContainerNumSlots or GetContainerNumSlots
  local info=api and api.GetContainerItemInfo or GetContainerItemInfo
  local link=api and api.GetContainerItemLink or GetContainerItemLink
  if not slots or not info or not link then return nil end
  local result={items={},slots={},money=GetMoney()}
  for bag=0,(NUM_BAG_SLOTS or 4) do
    for slot=1,(slots(bag) or 0) do
      local value=info(bag,slot)
      local count=type(value)=="table" and value.stackCount or select(2,info(bag,slot))
      local line=self:Line(link(bag,slot),count)
      if line then
        local key=self:Key(line)
        result.slots[bag .. ":" .. slot]=line
        if not result.items[key] then result.items[key]=C:Clone(line); result.items[key].quantity=0 end
        result.items[key].quantity=result.items[key].quantity+count
      end
    end
  end
  return result
end
function L:Delta(before,after)
  local changes,seen={},{}
  for key,line in pairs(before.items) do
    local count=(after.items[key] and after.items[key].quantity or 0)-line.quantity
    if count~=0 then changes[#changes+1]={line=C:Clone(line),delta=count} end
    seen[key]=true
  end
  for key,line in pairs(after.items) do if not seen[key] then changes[#changes+1]={line=C:Clone(line),delta=line.quantity} end end
  return changes
end
function L:Emit(kind,fields)
  if C.collectorOnly then
    if (kind=="craft" or kind=="disenchant") and C.CollectorStatus then
      C.CollectorStatus:Observe({kind=kind,confirmation="client",observed_at=math.floor(C:Now()),
        inputs=fields and fields.inputs,outputs=fields and fields.outputs,details=fields and fields.details})
    end
    return
  end
  if not self.db then return end
  if not C:MarketBindingMatches(self.market) then self.db.unbound=true; return end
  fields=fields or {}
  if C.Production then C.Production:Enrich(kind,fields) end
  if #self.db.events>=5000 then
    self.db.overflow=true; self.db.missed=(self.db.missed or 0)+1
    return
  end
  self.db.sequence=self.db.sequence+1
  local event={format_version=1,event_id=C.db.device_id .. ":ledger:" .. self.db.sequence,
    workspace_id=C.db.workspace_id or "unbound",market=self.market,realm=realm(),character=character(),
    observed_at=math.floor(C:Now()),sequence=self.db.sequence,kind=kind,confirmation="client",source="client",inputs={},outputs={}}
  for key,value in pairs(fields or {}) do event[key]=C:Clone(value) end
  if not event.market then return end
  self.db.events[#self.db.events+1]=event
  if #self.db.outbox<500 then self.db.outbox[#self.db.outbox+1]=event end
  self.db.remaining=math.max(0,#self.db.events-#self.db.outbox)
  if C.Production then C.Production:Observe(event) end
  if C.CollectorStatus then C.CollectorStatus:Observe(event) end
  return event
end
function L:Issue(reason,details)
  return self:Emit("issue",{confirmation="review",reason=reason,details=details or {}})
end
function L:Context(item,output)
  local found
  for _,task in pairs(C.db.tasks) do
    if task.market==self.market and not task.hidden then
      local matches=output and task.item_id==item
      if not output then
        for _,line in ipairs(task.lines or {}) do if line.item_id==item then matches=true end end
      end
      if matches then if found then return {} end; found=task end
    end
  end
  return found and {task_id=found.task_id,strategy_id=found.strategy_id,
    details={predicted_profit_copper=found.predicted_profit_copper}} or {}
end
function L:Purchase(origin,item,quantity,cost,fields)
  if not self.db or not C:Integer(cost,true) then return end
  self.db.purchase_origins=self.db.purchase_origins or {}
  if self.db.purchase_origins[origin] then return end
  local line=self:Line(item,quantity); if not line then return end
  fields=C:Clone(fields or {}); fields.outputs={line}; fields.money_copper=cost
  fields.source=fields.source or "auction"; fields.details=fields.details or {}; fields.details.origin_id=origin
  local key=fields.details.item_key
  if key and C.Adapter:Equipment(line.item_id) and line.link=="" then line.variant=(key.itemLevel or 0) .. ":" .. (key.itemSuffix or 0) end
  local event=self:Emit("purchase",fields)
  if event then
    self.db.purchase_origins[origin]=event.event_id
    if fields.source=="auction" then
      self.db.receivable[#self.db.receivable+1]={item_id=line.item_id,quantity=quantity,cost=cost,
        character=character(),realm=realm(),event_id=event.event_id}
    end
  end
  return event
end
function L:TaskPurchase(event,task)
  if event.kind~="purchase" or event.state~="bought" then return end
  local pending=C.db.pending
  local line=event.item_id
  if pending and pending.item_link then line=pending.item_link end
  self:Purchase("task:" .. event.event_id,line,event.quantity,event.cost_copper,{source="auction",
    task_id=task.task_id,strategy_id=task.strategy_id,confirmation=event.details.manually_confirmed and "manual" or "client",
    details={predicted_profit_copper=task.predicted_profit_copper,opportunity_id=task.opportunity_id,item_key=pending and pending.item_key}})
end
function L:Opening()
  if not C:MarketBindingMatches(self.market) then return end
  local snapshot=self:Snapshot(); if not snapshot then return end
  self.bags=snapshot
  self.db.unbound=false
  local key=realm() .. ":" .. character()
  if not self.db.initialized_characters[key] then
    for _,line in pairs(snapshot.items) do
      local incoming=false
      for _,credit in ipairs(self.db.transfer_receivable or {}) do
        if credit.target==character() and credit.realm==realm() and credit.line.quantity>0 and self:Key(credit.line)==self:Key(line) then incoming=true end
      end
      if incoming then self:Issue("Начальный запас пересекается с учтённым переносом: требуется сверка",{outputs={line}})
      else self:Emit("opening",{outputs={line},source="initial_bags",reason="Начальный запас: себестоимость неизвестна"}) end
    end
    self.db.initialized_characters[key]=true
  end
end
function L:Initialize()
  if C.collectorOnly then
    self.db={};self.market=C.db.current_market
    if not self.hooked then self.hooked=true;self:InitializePublicCapture() end
    return
  end
  local incoming=CennikImport and CennikImport.accounting
  local workspace=C.db.workspace_id or "unbound"
  C.db.accounting_profiles=C.db.accounting_profiles or {}
  if C.db.accounting and C.db.accounting.workspace_id~=workspace then
    C.db.accounting_profiles[C.db.accounting.workspace_id or "unbound"]=C.db.accounting
    C.db.accounting=C.db.accounting_profiles[workspace]
  end
  C.db.accounting=C.db.accounting or {events={},outbox={},sequence=0,receivable={},purchase_origins={},initialized_characters={}}
  self.db=C.db.accounting; self.db.workspace_id=workspace
  self.db.receivable=self.db.receivable or {}; self.db.outbox={}
  self.market=incoming and incoming.market or C.db.binding and C.db.binding.market or C.db.current_market
  self.db.unbound=not C:MarketBindingMatches(self.market)
  local ack={}
  if incoming and incoming.workspace_id==workspace then
    for _,id in ipairs(incoming.acknowledged_event_ids or {}) do ack[id]=true end
  end
  local kept={}
  for _,event in ipairs(self.db.events) do if not ack[event.event_id] then kept[#kept+1]=event end end
  self.db.events=kept
  for i=1,math.min(500,#kept) do self.db.outbox[i]=kept[i] end
  self.db.remaining=math.max(0,#kept-500)
  if self.db.interrupted then self:Issue("Операция прервана перезагрузкой; результат требует сверки",self.db.interrupted); self.db.interrupted=nil end
  for _,pending in pairs(self.db.mail_pending or {}) do self:Issue("Получение почты прервано reload; требуется сверка",{mail=pending.mail}) end
  self.db.mail_pending=nil; self.mailPending={}
  C.db.accounting_characters=C.db.accounting_characters or {}
  C.db.accounting_characters[realm() .. ":" .. character()]=true
  self:Opening()
  if self.InitializeCapture and not self.hooked then self.hooked=true; self:InitializeCapture() end
end
