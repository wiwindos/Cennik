Game={now=1791890000,clock=0,timers={},frames={},calls={},commodity={},items={},gear={},full=true,hardware=false,money=1000000,
  missingInfo={},localized={},itemRequests={}}
local function noop() end
function GetServerTime() return Game.now end
function GetTime() return Game.clock end
function GetMoney() return Game.money end
function GetMoneyString(value) return tostring(value) .. "c" end
function date(format,value) return tostring(value) end
function GetRealmName() return "FixtureRealm" end
function UnitFactionGroup() return "Alliance" end
function UnitName() return "FixturePlayer" end
SlashCmdList={}
function CreateFrame(kind,name,parent,template)
  local f={kind=kind,scripts={},events={},shown=true,parent=parent,points={}}
  table.insert(Game.frames,f)
  local methods={SetScript=function(self,key,fn) self.scripts[key]=fn end, RegisterEvent=function(self,e) self.events[e]=true end,
    Show=function(self)
      local was=self.shown; self.shown=true
      if not was and self.scripts.OnShow then self.scripts.OnShow(self) end
    end,
    Hide=function(self) self.shown=false end, IsShown=function(self) return self.shown end,
    IsVisible=function(self)
      local current=self
      for _=1,100 do
        if not current.shown then return false end
        local parent=rawget(current,"parent")
        if parent and parent.kind=="ScrollFrame" and rawget(parent,"scrollChild")==current and #current.points==0 then return false end
        current=rawget(current,"parent")
        if not current then return true end
      end
      error("Frame parent cycle")
    end,
    GetScript=function(self,key) return self.scripts[key] end,
    SetSize=function(self,w,h) self.width,self.height=w,h end,
    SetWidth=function(self,w) self.width=w end,SetHeight=function(self,h) self.height=h end,
    SetClipsChildren=function(self,value) self.clipsChildren=value end,
    GetHeight=function(self) return rawget(self,"height") or 0 end,
    ClearAllPoints=function(self) self.points={} end,
    SetPoint=function(self,...) table.insert(self.points,{...}) end,
    GetNumPoints=function(self) return #self.points end,
    GetStringHeight=function(self)
      local text=rawget(self,"text") or ""
      local _,breaks=text:gsub("\n","")
      return (breaks+1)*14
    end,
    SetScrollChild=function(self,child)
      -- Setting the same child does not rebuild anchors cleared by the caller.
      if rawget(self,"scrollChild")==child then return end
      self.scrollChild=child; child:SetParent(self)
      child:ClearAllPoints(); child:SetPoint("TOPLEFT",self,"TOPLEFT",0,0)
    end,
    GetScrollChild=function(self) return rawget(self,"scrollChild") end,
    SetVerticalScroll=function(self,value) self.verticalScroll=value end,
    GetVerticalScroll=function(self) return rawget(self,"verticalScroll") or 0 end,
    GetVerticalScrollRange=function(self)
      local child=rawget(self,"scrollChild")
      return child and math.max(0,child:GetHeight()-self:GetHeight()) or 0
    end,
    UpdateScrollChildRect=function(self) self.rectUpdates=(rawget(self,"rectUpdates") or 0)+1 end,
    SetText=function(self,v) self.text=v end,GetText=function(self) return self.text or "" end,
    SetChecked=function(self,v) self.checked=v end,GetChecked=function(self) return self.checked end,
    SetFocus=function(self) Game.focused=self end,ClearFocus=function(self) if Game.focused==self then Game.focused=nil end end,
    HasFocus=function(self) return Game.focused==self end,
    Enable=function(self) self.enabled=true end,Disable=function(self) self.enabled=false end,
    SetEnabled=function(self,v) self.enabled=not not v end,IsEnabled=function(self) return self.enabled~=false end,
    SetParent=function(self,v) self.parent=v end,IsObjectType=function() return true end,
    CreateFontString=function(self) return CreateFrame("FontString",nil,self) end,
    CreateTexture=function(self) return CreateFrame("Texture",nil,self) end,
    SetColorTexture=function(self,r,g,b,a) self.color={r,g,b,a} end,
    GetChildren=function() end,GetRegions=function() end}
  setmetatable(f,{__index=function(_,key) return methods[key] or noop end})
  return f
end
AuctionHouseFrame=CreateFrame("Frame")
local lib={tabs={}}
function lib:CreateTab(id,frame) self.tabs[id]=frame; frame:Hide() end
function lib:SetSelected(id)
  for _,frame in pairs(self.tabs) do frame:Hide() end
  self.tabs[id]:Show()
end
LibStub=function() return lib end
Auctionator={API={v1={
    MultiSearch=function(caller,terms) Game.shoppingMode="plain"; Game.shopping=terms end,
    MultiSearchExact=function(caller,terms)
      Game.shoppingMode="exact"; Game.shopping={}
      for _,term in ipairs(terms) do table.insert(Game.shopping,'"' .. term .. '"') end
    end}},AH={Queue={queue={}}},
  Shopping={Tab={Events={SearchStart="start",SearchEnd="end"}}},FullScan={Events={ScanStart="fullstart",ScanComplete="fullend",ScanFailed="fullfailed"}},
  EventBus={Register=noop}}
function hooksecurefunc(table,name,hook)
  if type(table)=="string" then hook=name; name=table; table=_G end
  local original=table[name]
  table[name]=function(...) local result=original(...); hook(...); return result end
end
Enum={AuctionHouseSortOrder={Price=1}}
C_Item={GetItemInfoInstant=function(id)
    if Game.missingInfo[id] then return nil end
    return id,"Type","Sub",Game.gear[id] and "INVTYPE_HEAD" or "",1,Game.gear[id] and 4 or 7
  end,
  GetItemInfo=function(id)
    if Game.missingInfo[id] then return nil end
    local name=Game.localized[id] or ("Item " .. id)
    local item=type(id)=="number" and id or tonumber(id:match("item:(%d+)"))
    local sell=Game.vendor and Game.vendor[id]
    if sell==nil then sell=Game.vendor and Game.vendor[item] end
    return name,"|Hitem:" .. id .. "|h[" .. name .. "]|h",nil,nil,nil,nil,nil,nil,nil,12345,sell
  end,
  RequestLoadItemDataByID=function(id) table.insert(Game.itemRequests,id) end}
C_Timer={After=function(seconds,fn) table.insert(Game.timers,{due=Game.clock+seconds,fn=fn}) end}
function Game.advance(seconds)
  local target=Game.clock+seconds
  for iteration=1,10000 do
    local best,index
    for i,t in ipairs(Game.timers) do if t.due<=target and (not best or t.due<best.due) then best,index=t,i end end
    if not best then break end
    Game.now=Game.now+(best.due-Game.clock); Game.clock=best.due; table.remove(Game.timers,index); best.fn()
  end
  Game.now=Game.now+(target-Game.clock); Game.clock=target
end
function Game.event(event,...)
  for _,frame in ipairs(Game.frames) do if frame.events[event] and frame.scripts.OnEvent then frame.scripts.OnEvent(frame,event,...) end end
end
function Game.click(fn) Game.hardware=true; local result=fn(); Game.hardware=false; return result end
local function purchase(kind,...)
  if not Game.hardware then error("Protected action requires a real hardware event") end
  table.insert(Game.calls,{kind=kind,args={...}})
end
C_AuctionHouse={MakeItemKey=function(id) return {itemID=id,itemLevel=0,itemSuffix=0,battlePetSpeciesID=0} end,
  GetItemKeyInfo=function() return {} end, IsThrottledMessageSystemReady=function() return not Game.throttled end,
  SendSearchQuery=function(key) table.insert(Game.calls,{kind="search",key=key}) end,
  HasFullCommoditySearchResults=function() return Game.full end,HasFullItemSearchResults=function() return Game.full end,
  GetNumCommoditySearchResults=function(id) return #(Game.commodity[id] or {}) end,
  GetCommoditySearchResultInfo=function(id,index) return Game.commodity[id][index] end,
  GetNumItemSearchResults=function(key) return #(Game.items[key.itemID] or {}) end,
  GetItemSearchResultInfo=function(key,index) return Game.items[key.itemID][index] end,
  GetAuctionInfoByID=function(id)
    for _,rows in pairs(Game.items) do for _,row in ipairs(rows) do if row.auctionID==id then return row end end end
  end,
  RequestMoreCommoditySearchResults=noop,RequestMoreItemSearchResults=noop,
  StartCommoditiesPurchase=function(...) purchase("start",...) end,
  ConfirmCommoditiesPurchase=function(...) purchase("confirm",...) end,
  CancelCommoditiesPurchase=noop,PlaceBid=function(...) purchase("bid",...) end}
