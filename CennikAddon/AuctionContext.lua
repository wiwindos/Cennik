local _,C=...
local A={}
C.AuctionContext=A
local function safe(fn,...)
  if type(fn)~="function" then return end
  local ok,value=pcall(fn,...)
  if ok and not (issecretvalue and issecretvalue(value)) then return value end
end
function A:Initialize()
  C.db.auctioneer_proposals=C.db.auctioneer_proposals or {}
  self.registry=CennikImport and CennikImport.auctioneer_registry or {}
  self.current=nil;self.session=nil
  if C.Adapter:Open() then self:Open() end
end
function A:Read()
  if not C.Adapter:Open() then return end
  local guid=safe(UnitGUID,"npc")
  if type(guid)~="string" then return end
  local fields={};for f in guid:gmatch("[^-]+") do fields[#fields+1]=f end
  if fields[1]~="Creature" and fields[1]~="Vehicle" then return end
  local npc=tonumber(fields[6]);local name=safe(UnitName,"npc")
  local map=C_Map and safe(C_Map.GetBestMapForUnit,"player")
  local pos=map and C_Map.GetPlayerMapPosition and safe(C_Map.GetPlayerMapPosition,map,"player")
  local x,y
  if pos and pos.GetXY then
    local ok,px,py=pcall(pos.GetXY,pos)
    if ok and not (issecretvalue and (issecretvalue(px) or issecretvalue(py))) then x,y=px,py end
  end
  if not npc or type(name)~="string" or type(map)~="number" or type(x)~="number" or type(y)~="number" then return end
  if x~=x or y~=y or x<0 or x>1 or y<0 or y>1 then return end
  local info=C_Map.GetMapInfo and safe(C_Map.GetMapInfo,map)
  local build=GetBuildInfo and select(2,GetBuildInfo())
  return {npc_id=npc,npc_name=name,realm=GetRealmName(),map_id=map,map_name=info and info.name or "",locale=C:ClientLocale(),
    server_id=C.db.server_binding and C.db.server_binding.id or "",market=C.db.server_binding and C.db.server_binding.market or "",
    x=math.floor(x*1000+.5)/10,y=math.floor(y*1000+.5)/10,observed_at=math.floor(C:Now()),build=tostring(build or "")}
end
function A:Resolve()
  local data=self:Read();local registry=self.registry or {}
  if CennikImport and (CennikImport.market_catalog or CennikImport.market_binding) and not C.db.server_binding then return nil,data end
  if not data or registry.format_version~=1 or not registry.fetched_at or C:Now()<registry.fetched_at-300 or C:Now()-registry.fetched_at>86400 then return nil,data end
  for _,entry in ipairs(registry.entries or {}) do
    local d=entry.data
    if entry.approved and d and d.npc_id==data.npc_id and d.realm==data.realm and d.map_id==data.map_id
      and (d.server_id or "")==data.server_id and (d.market or "")==data.market
      and math.abs(d.x-data.x)<=2 and math.abs(d.y-data.y)<=2
      and (entry.auction_type=="ordinary" or entry.auction_type=="neutral") then
      return {scope_id=entry.scope_id,revision=entry.revision,auction_type=entry.auction_type,
        server_id=data.server_id,npc_id=data.npc_id,realm=data.realm,map_id=data.map_id,x=data.x,y=data.y},data
    end
  end
  return nil,data
end
function A:Check(purpose)
  local context=self:Resolve()
  if not context then return false,"Тип аукциона не подтверждён владельцем. Выберите предполагаемый тип в /cennik collector; сбор и закупки заблокированы","AUCTION_CONTEXT_UNKNOWN" end
  if context.auction_type=="neutral" and purpose~="collect" then
    return false,"Межфракционный аукцион: разрешён только отдельный архивный скан, закупки Cennik заблокированы","AUCTION_ARCHIVE_ONLY"
  end
  return true,context
end
function A:Snapshot()
  if not self.session and C.Adapter:Open() then self:Open() end
  local ok,context=self:Check("collect")
  if ok then context.session_id=self.session;return C:Clone(context) end
end
function A:Matches(context)
  local now=self:Snapshot()
  return now and context and now.scope_id==context.scope_id and now.revision==context.revision and now.auction_type==context.auction_type
    and context.session_id==self.session
end
function A:Open()
  if self.session then return end
  C.db.auctioneer_session_sequence=(C.db.auctioneer_session_sequence or 0)+1
  self.session=C.db.device_id .. ":auction:" .. tostring(C:Now()) .. ":" .. C.db.auctioneer_session_sequence
  self.current=self:Snapshot()
end
function A:Propose(kind)
  if kind~="ordinary" and kind~="neutral" then return false end
  local existing=self:Resolve()
  local data=self:Read()
  if not data then
    if C.CollectorStatus then C.CollectorStatus:Set("AUCTION_CONTEXT_UNKNOWN","Не удалось прочитать ID, имя или координаты аукциониста") end
    return false
  end
  local key=tostring(data.npc_id) .. ":" .. data.realm .. ":" .. data.map_id
  local count=0;for _ in pairs(C.db.auctioneer_proposals) do count=count+1 end
  if not C.db.auctioneer_proposals[key] and count>=100 then
    if C.CollectorStatus then C.CollectorStatus:Set("OBSERVATION_LIMIT","Сохранено 100 заявок аукционистов; передайте данные приложению") end
    return false
  end
  data.proposed_type=kind
  C.db.auctioneer_proposals[key]=data
  if C.CollectorStatus then C.CollectorStatus:Set("AUCTION_PROPOSAL_PENDING",
    existing and "Новое предложение сохранено; до решения владельца действует прежний подтверждённый тип. Передача после /reload или выхода"
    or "Заявка сохранена. После /reload или выхода приложение передаст её владельцу; до одобрения операции заблокированы") end
  return true
end
function A:Summary()
  local context,data=self:Resolve()
  local lines={"Аукцион: " .. (context and (context.auction_type=="neutral" and "межфракционный · только архив" or "обычный · подтверждён владельцем") or "неизвестный · сбор и закупки заблокированы")}
  if data then
    lines[#lines+1]=data.npc_name .. " · NPC #" .. data.npc_id
    lines[#lines+1]=data.map_name .. " (#" .. data.map_id .. ") · " .. data.x .. ", " .. data.y .. " — положение игрока при общении"
    local key=tostring(data.npc_id) .. ":" .. data.realm .. ":" .. data.map_id
    local proposal=C.db.auctioneer_proposals[key]
    if proposal then lines[#lines+1]="Предложено игроком: " .. (proposal.proposed_type=="neutral" and "межфракционный" or "обычный") .. "; предложение не является подтверждением" end
  else lines[#lines+1]="Откройте аукцион у NPC, чтобы прочитать его имя и местоположение" end
  return table.concat(lines,"\n")
end
local frame=CreateFrame("Frame")
frame:RegisterEvent("AUCTION_HOUSE_SHOW");frame:RegisterEvent("AUCTION_HOUSE_CLOSED")
frame:SetScript("OnEvent",function(_,event)
  if not C.db then return end
  if event=="AUCTION_HOUSE_SHOW" then A:Open()
  else A.session=nil;A.current=nil end
end)
