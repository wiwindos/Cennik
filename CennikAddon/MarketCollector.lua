local _, C = ...
local M = {elapsed=0,interval=15*60,maxQueue=3,maxBytes=48000000,maxTotalBytes=128000000,maxRows=2000000,maxVariants=300000}
C.MarketCollector=M

local function stringBytes(value)
  local text=value or ""
  local _,escaped=text:gsub('[%z\1-\31\\"]','')
  return #text+escaped*5+2
end
local function variantBytes(v)
  return stringBytes(v.link)+stringBytes(v.name)+128
end
function M:ScanBytes(scan)
  if scan.packed_bytes then return scan.packed_bytes end
  self.sizes=self.sizes or setmetatable({},{__mode="k"})
  if not self.sizes[scan] then
    local bytes=#(scan.data or "")+4096
    for _,v in ipairs(scan.variants or {}) do bytes=bytes+variantBytes(v) end
    self.sizes[scan]=bytes
  end
  return self.sizes[scan]
end
function M:QueuedBytes()
  local bytes=0
  for _,scan in ipairs(C.db.market_scans or {}) do
    bytes=bytes+self:ScanBytes(scan)
  end
  return bytes
end

function M:Notice(message)
  self.message=message
  if C.UI and C.UI.section=="Market" then C:Refresh() end
end
function M:Failed(code, detail, reported)
  C.db.collector.last_scan={full=false,count=0,reported_count=reported or 0,code=code,
    error=detail or (C.CollectorStatus.codes[code] or {})[2] or code}
  if C.CollectorStatus then C.CollectorStatus:Set(code,detail) end
end
function M:Reserve(bytes, includeCount)
  -- Keep the newest observations, within both count and byte bounds.
  while #C.db.market_scans>0 and ((includeCount and #C.db.market_scans>=self.maxQueue) or self:QueuedBytes()+bytes>self.maxTotalBytes) do
    table.remove(C.db.market_scans,1)
    C.db.collector.rotated=(C.db.collector.rotated or 0)+1
  end
end
function M:Initialize()
  C.db.market_scans=C.db.market_scans or {}
  C.db.collector=C.db.collector or {enabled=false,next_at=0,sequence=0}
  self.openedAt=C.Adapter:Open() and GetTime() or nil
  local imported=CennikImport or {}
  local ack={}
  for _,id in ipairs(imported.acknowledged_scan_ids or {}) do ack[id]=true end
  local pending={}
  for _,scan in ipairs(C.db.market_scans) do if not ack[scan.scan_id] then table.insert(pending,scan) end end
  C.db.market_scans=pending
  if C.db.collector.last_scan and ack[C.db.collector.last_scan.scan_id] then C.db.collector.last_scan.saved=true end
  if type(imported.market_collector)=="table" then
    local config=imported.market_collector
    if type(config.market)=="string" then
      C.db.collector.market=config.market
      local signature=tostring(config.workspace_id) .. ":" .. config.market .. ":" .. tostring(config.auto_scan)
      if C.db.collector.applied_config~=signature then
        C.db.collector.enabled=config.auto_scan==true
        C.db.collector.applied_config=signature
      end
      C.db.collector.workspace_id=config.workspace_id
      C.db.current_market=C.db.current_market or config.market
    end
  end
  self:Notice("Снимков ожидает записи/подтверждения: " .. #pending)
  if not self.hooked and C_AuctionHouse and C_AuctionHouse.ReplicateItems then
    self.hooked=true
    hooksecurefunc(C_AuctionHouse,"ReplicateItems",function()
      if not C.db or not C.db.collector then return end
      if not (C.CollectorStatus and C.CollectorStatus:IsEnabled()) and not M.ownRequest then return end
      local context=C.AuctionContext:Snapshot()
      if not context then
        if C.CollectorStatus then C.CollectorStatus:Set("AUCTION_CONTEXT_UNKNOWN") end
        return
      end
      M.auction_context=context
      C.db.collector.next_at=C:Now()+M.interval
      M.requested_at=C:Now()
      if C.CollectorStatus then C.CollectorStatus:Set(M.ownRequest and "REQUESTED" or "SHARED_SCAN") end
      M:Notice("Ожидание массового ответа сервера")
    end)
  end
end
function M:Available()
  if C.Automation and C.Automation.quiescing then return false,"Подготовка безопасного сохранения","BUSY" end
  local state=C.db.collector
  if not state or not C:MarketBindingMatches(state.market or C.db.current_market) then return false,"Подтвердите рынок персонажа","UNBOUND" end
  if not C.Adapter:Open() then return false,"Откройте аукцион","WAIT_AUCTION" end
  local contextOK,contextReason,contextCode=C.AuctionContext:Check("collect")
  if not contextOK then return false,contextReason,contextCode end
  if self.requested_at or self.packing or C.Adapter.fullScan or C.Adapter.shopping or C.Scanner.active or C.db.pending then return false,"Аукцион занят","BUSY" end
  if C:Now()<(state.next_at or 0) then return false,"Следующий запрос через " .. math.ceil((state.next_at-C:Now())/60) .. " мин","COOLDOWN" end
  local scanner=Auctionator and Auctionator.State and Auctionator.State.FullScanFrameRef
  if not scanner or type(scanner.InitiateScan)~="function" or type(scanner.CanInitiate)~="function" then return false,"Auctionator не предоставляет совместимый полный скан","UNSUPPORTED" end
  if not scanner:CanInitiate() or not C.Adapter:Ready("collector") then return false,"Auctionator ещё не готов к полному скану","BUSY" end
  return true,scanner
end
function M:Start()
  local ok,scanner,code=self:Available()
  if not ok then self:Notice(scanner);if C.CollectorStatus then C.CollectorStatus:Set(code,scanner) end; return false,scanner end
  self.requested_at=C:Now()
  self.auction_context=C.AuctionContext:Snapshot()
  C.db.collector.next_at=C:Now()+self.interval
  self.ownRequest=true
  local success,reason=pcall(scanner.InitiateScan,scanner)
  self.ownRequest=false
  if not success then self.requested_at=nil;self:Notice("Ошибка Auctionator: " .. tostring(reason));self:Failed("SCAN_FAILED",tostring(reason)); return false,reason end
  self:Notice("Общий массовый запрос Auctionator / Cennik отправлен")
  return true
end
function M:Pack()
  local p=self.packing
  if not p then return end
  if not C.Adapter:Open() then p.error="Аукцион закрыт во время обработки"; self:Finish(); return end
  if not C.AuctionContext:Matches(self.auction_context) then
    self.packing=nil;self.requested_at=nil
    self:Failed("AUCTION_CONTEXT_UNKNOWN","Контекст изменился во время обработки; результат не используется",p.count)
    return
  end
  local ok,reason=pcall(function()
    local clock=GetTimePreciseSec or GetTime
    local deadline=clock()+.004
    local stop=math.min(p.index+500,p.count)
    for index=p.index,stop-1 do
      local info={C_AuctionHouse.GetReplicateItemInfo(index)}
      local item,quantity,buyout,bid=info[17],info[3],info[10] or 0,info[11] or 0
      if C:Integer(item) and C:Integer(quantity) and C:Integer(buyout,true) and C:Integer(bid,true) then
        local link=C_AuctionHouse.GetReplicateItemLink(index)
        local variantKey=link or ("unknown:" .. item)
        local variant=p.keys[variantKey]
        if not variant then
          if #p.variants>=self.maxVariants then error("Слишком много вариантов предметов; сохранена прочитанная часть") end
          variant=#p.variants+1; p.keys[variantKey]=variant
          local key=C_AuctionHouse.MakeItemKey(item)
          local keyInfo=C_AuctionHouse.GetItemKeyInfo(key)
          local get=C_Item and C_Item.GetItemInfo or GetItemInfo
          local facts=type(get)=="function" and {get(link or item)} or {}
          p.variants[variant]={link=link,name=info[1] or facts[1],
            divisible=keyInfo and keyInfo.isCommodity,vendor_sell=facts[11]}
          p.bytes=p.bytes+variantBytes(p.variants[variant])
        end
        local remaining=C_AuctionHouse.GetReplicateItemTimeLeft(index) or 0
        local owner=info[14]
        local ownership=type(owner)=="string" and owner~="" and (owner==UnitName("player") and 1 or 2) or 0
        local row=string.format("%.0f:%.0f:%.0f:%.0f:%.0f:%d:%d",item,quantity,buyout,bid,remaining,variant,ownership)
        p.bytes=p.bytes+#row+1
        if p.bytes>p.limit then error("Скан превышает размер поддерживаемой очереди; сохранена прочитанная часть") end
        table.insert(p.parts,row)
        p.rows=p.rows+1
        if #p.parts>=2000 then self:FlushRows(p) end
      else p.skipped=p.skipped+1 end
      p.index=index+1
      if clock()>=deadline then break end
    end
  end)
  if not ok then p.error=tostring(reason); self:Finish(); return end
  if p.index>=p.count then self:Finish()
  else
    if not p.progress_at or GetTime()-p.progress_at>=.5 then
      p.progress_at=GetTime()
      if C.CollectorStatus then C.CollectorStatus:Set("PACKING",string.format("%d / %d строк · %.1f%% · %.1f МБ",p.index,p.count,100*p.index/p.count,p.bytes/1000000)) end
    end
    C_Timer.After(0,function() if M.packing==p then M:Pack() end end)
  end
end
function M:FlushRows(p)
  p.chunks=p.chunks or {}
  if #p.parts>0 then p.chunks[#p.chunks+1]=table.concat(p.parts,";");p.parts={} end
end
function M:Finish()
  local p=self.packing
  if not p then return end
  C.db.collector.sequence=(C.db.collector.sequence or 0)+1
  local rows=p.rows or #p.parts
  self:FlushRows(p)
  p.keys=nil
  local _,build=GetBuildInfo()
  local scan={format_version=1,scan_id=C.db.device_id .. ":market:" .. C:Now() .. ":" .. C.db.collector.sequence,
    market=self.auction_context and self.auction_context.auction_type=="neutral" and (C.db.collector.market or C.db.current_market):gsub("%.alliance%.",".neutral."):gsub("%.horde%.",".neutral.") or C.db.collector.market or C.db.current_market,
    realm=GetRealmName(),faction=string.lower(UnitFactionGroup("player") or ""),
    observed_at=p.observed_at,requested_at=self.requested_at,received_at=p.observed_at,build=build,
    count=rows,reported_count=p.count,skipped=p.skipped,full=not p.error and p.skipped==0 and rows==p.count,
    error=p.error,variants=p.variants,data=table.concat(p.chunks,";"),packed_bytes=(p.bytes or 0)+4096,workspace_id=C.db.collector.workspace_id,
    auction_context=C:Clone(self.auction_context)}
  if scan.full then self:Reserve((p.bytes or 0)+4096,true) end
  if scan.full or (#C.db.market_scans<self.maxQueue and self:QueuedBytes()+(p.bytes or 0)+4096<=self.maxTotalBytes) then
    table.insert(C.db.market_scans,scan)
  else
    C.db.collector.skipped_partial=(C.db.collector.skipped_partial or 0)+1
  end
  if C.CollectorStatus then C.CollectorStatus:Finished(scan) end
  C.db.collector.last_scan={scan_id=scan.scan_id,full=scan.full,count=scan.count,reported_count=scan.reported_count,error=scan.error,
    code=not scan.full and C.CollectorStatus.db.current.code or nil}
  self.packing=nil; self.requested_at=nil
  self.emptyResponse=nil
  self:Notice((scan.full and "Полный снимок готов: " or "НЕПОЛНЫЙ снимок: ") .. scan.count .. " / " .. scan.reported_count .. " строк. " ..
    (scan.full and "Можно записать / reload" or "Не заменяет цены рынка; требуется повторный полный скан") ..
    (scan.error and (". " .. scan.error) or ""))
  print("Cennik: " .. self.message)
end
function M:Receive()
  if not self.requested_at or self.packing or not C.Adapter:Open() or not C.db.collector or not C:MarketBindingMatches(C.db.collector.market or C.db.current_market) then return end
  if not C.AuctionContext:Matches(self.auction_context) then
    self.requested_at=nil;self:Failed("AUCTION_CONTEXT_UNKNOWN","Ответ относится к другому или неподтверждённому аукциону")
    return
  end
  local count=C_AuctionHouse.GetNumReplicateItems()
  if not C:Integer(count,true) then self:Notice("API не сообщил количество строк"); return end
  if count==0 then
    if not self.emptyResponse then self:Notice("Пустое событие API; ожидаем данные. Оно не считается полным сканом") end
    self.emptyResponse=true
    return
  end
  if count>self.maxRows then
    self.requested_at=nil
    self:Failed("SCAN_TOO_LARGE",tostring(count) .. " строк: превышен предел 2 млн. Предыдущие полные снимки сохранены",count)
    return
  end
  self.packing={index=0,count=count,observed_at=C:Now(),parts={},chunks={},rows=0,keys={},variants={},skipped=0,bytes=0,limit=self.maxBytes-4096}
  if C.CollectorStatus then C.CollectorStatus:Set("PACKING",tostring(count) .. " строк") end
  self:Pack()
end
function M:Tick(elapsed)
  self.elapsed=self.elapsed+elapsed
  if self.elapsed<1 then return end
  self.elapsed=0
  if not C.db or not C.db.collector then return end
  if self.requested_at and not self.packing and C:Now()-self.requested_at>120 then
    if self.emptyResponse then
      self.packing={count=0,parts={},variants={},skipped=0,observed_at=C:Now(),error="API не вернул строки за 120 секунд; пустой ответ не подтверждает пустой рынок"}
      self:Finish()
    else self.requested_at=nil; self:Notice("Ответ сервера не пришёл. Соблюдаем срок следующего запроса");self:Failed("SCAN_TIMEOUT") end
  end
  if C.db.collector.enabled and not (C.CollectorStatus and C.CollectorStatus.db and C.CollectorStatus.db.paused) and not self.requested_at then
    if C.Adapter:Open() and not self.openedAt then self.openedAt=GetTime() end
    local available,reason,code=self:Available()
    if C.Adapter:Open() and GetTime()-(self.openedAt or GetTime())<3 then
      if C.CollectorStatus then C.CollectorStatus:Set("OPEN_DELAY") end
    elseif available then self:Start()
    elseif C.CollectorStatus then
      local current=C.CollectorStatus.db and C.CollectorStatus.db.current
      if not current or current.level~="error" or C:Now()-current.at>10 then
        if code=="COOLDOWN" then
          local latest=C.db.collector.last_scan or C.db.market_scans[#C.db.market_scans]
          if latest and latest.full then C.CollectorStatus:Set(latest.saved and "SCAN_SAVED" or "READY")
          elseif latest then C.CollectorStatus:Set(latest.code or "SCAN_PARTIAL",tostring(latest.count or 0) .. " / " .. tostring(latest.reported_count or 0) .. " строк. " .. (latest.error or "Неполное покрытие"))
          elseif not current or current.level~="error" then C.CollectorStatus:Set(code,reason) end
        else C.CollectorStatus:Set(code,reason) end
      end
    end
  elseif not C.db.collector.enabled and not self.requested_at and C.CollectorStatus then
    C.CollectorStatus:Set(C.CollectorStatus:IsEnabled() and "PASSIVE" or "DISABLED")
  end
  if C.CollectorUI then C.CollectorUI:Refresh() end
end
local frame=CreateFrame("Frame")
frame:RegisterEvent("REPLICATE_ITEM_LIST_UPDATE")
frame:RegisterEvent("AUCTION_HOUSE_CLOSED")
frame:RegisterEvent("AUCTION_HOUSE_SHOW")
frame:RegisterEvent("AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED")
frame:SetScript("OnUpdate",function(_,elapsed) M:Tick(elapsed) end)
frame:SetScript("OnEvent",function(_,event)
  if not C.db or not C.db.collector then return end
  if event=="AUCTION_HOUSE_SHOW" then M.openedAt=GetTime();if C.CollectorStatus then C.CollectorStatus:Set("OPEN_DELAY") end
  elseif event=="REPLICATE_ITEM_LIST_UPDATE" then M:Receive()
  elseif event=="AUCTION_HOUSE_CLOSED" then
    M.openedAt=nil
    if M.packing or M.requested_at then
      M:Failed("SCAN_INTERRUPTED")
      if M.packing then M.packing.error="Аукцион закрыт во время обработки"; M:Finish() end
      M.requested_at=nil;M.emptyResponse=nil
    end
  elseif event=="AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED" and M.requested_at then M.requested_at=nil; M:Notice("Сервер отклонил запрос. Повтор после разрешённого интервала");M:Failed("SCAN_REJECTED") end
end)
