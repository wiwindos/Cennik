-- Cennik profession observations. Independent implementation using WoW APIs.
local _,C=...
local P={}
C.Professions=P
local aliases={
  [164]="blacksmithing",[165]="leatherworking",[171]="alchemy",[197]="tailoring",
  [202]="engineering",[333]="enchanting",[185]="cooking",[129]="first aid",[186]="mining",[755]="jewelcrafting",
  ["Кузнечное дело"]="blacksmithing",["Кожевничество"]="leatherworking",["Алхимия"]="alchemy",
  ["Портняжное дело"]="tailoring",["Инженерное дело"]="engineering",["Наложение чар"]="enchanting",
  ["Кулинария"]="cooking",["Первая помощь"]="first aid",["Горное дело"]="mining",["Ювелирное дело"]="jewelcrafting"
}
local function key(name,id) return aliases[id] or aliases[name] or string.lower(name or "") end
local professions={};for _,value in pairs(aliases) do professions[value]=true end
local function positive(n) return type(n)=="number" and n>0 and n==math.floor(n) end
local function difficulty(value)
  local e=Enum and Enum.TradeskillRelativeDifficulty
  if e then
    if value==e.Optimal then return "orange" elseif value==e.Medium then return "yellow"
    elseif value==e.Easy then return "green" elseif value==e.Trivial then return "grey" end
  end
  return ({optimal="orange",medium="yellow",easy="green",trivial="grey"})[value] or "unknown"
end
function P:State(snapshot,name,rank,maximum,id)
  if not name or name=="" or name=="UNKNOWN" or type(rank)~="number" or not positive(maximum) or rank<0 or rank>maximum then return end
  local identity=key(name,id)
  local state=snapshot.professions[identity] or {recipes={},coverage="levels_only"}
  state.name,state.skill,state.max_skill,state.observed_at=name,rank,maximum,math.floor(C:Now())
  snapshot.professions[identity]=state
  return state
end
function P:Levels(snapshot)
  if GetProfessions and GetProfessionInfo then
    local ids={GetProfessions()}
    for _,id in pairs(ids) do
      local name,_,rank,maximum,_,_,skillID=GetProfessionInfo(id)
      self:State(snapshot,name,rank,maximum,skillID)
    end
  elseif GetNumSkillLines and GetSkillLineInfo then
    for i=1,GetNumSkillLines() do
      local name,header,_,rank,_,_,maximum=GetSkillLineInfo(i)
      if not header and professions[key(name)] then self:State(snapshot,name,rank,maximum) end
    end
  end
end
function P:Capture(silent)
  if not C.db or C.collectorOnly then return end
  local character,realm=UnitName("player"),GetRealmName()
  local identity=realm .. ":" .. character
  C.db.profession_profiles=C.db.profession_profiles or {}
  local snapshot=C.db.profession_profiles[identity] or {format_version=1,character=character,realm=realm,professions={}}
  snapshot.observed_at=math.floor(C:Now())
  snapshot.disenchant=IsPlayerSpell and not not IsPlayerSpell(13262) or IsSpellKnown and not not IsSpellKnown(13262) or false
  self:Levels(snapshot)
  local state,count=nil,0
  if GetTradeSkillLine then
    local name,rank,maximum=GetTradeSkillLine()
    state=self:State(snapshot,name,rank,maximum)
  elseif C_TradeSkillUI and C_TradeSkillUI.GetBaseProfessionInfo then
    local ok,info=pcall(C_TradeSkillUI.GetBaseProfessionInfo)
    if ok and info then state=self:State(snapshot,info.professionName,info.skillLevel,info.maxSkillLevel,info.professionID) end
  end
  if state and GetNumTradeSkills and GetTradeSkillInfo and GetTradeSkillRecipeLink then
    for index=1,math.min(5000,GetNumTradeSkills() or 0) do
      local name,kind=GetTradeSkillInfo(index)
      local link=GetTradeSkillRecipeLink(index)
      local spell=link and tonumber(link:match("enchant:(%d+)") or link:match("spell:(%d+)"))
      if spell and kind~="header" and kind~="subheader" then
        local output=GetTradeSkillItemLink and GetTradeSkillItemLink(index)
        local item=output and tonumber(output:match("item:(%d+)"))
        local qmin,qmax
        if GetTradeSkillNumMade then qmin,qmax=GetTradeSkillNumMade(index) end
        local inputs,complete={},true
        if not GetTradeSkillNumReagents then complete=false else
          for reagent=1,GetTradeSkillNumReagents(index) do
            local _,_,q=GetTradeSkillReagentInfo(index,reagent)
            local rlink=GetTradeSkillReagentItemLink(index,reagent)
            local rid=rlink and tonumber(rlink:match("item:(%d+)"))
            if positive(rid) and positive(q) then inputs[#inputs+1]={item_id=rid,quantity=q} else complete=false end
          end
        end
        local recipe={spell_id=spell,name=name or "",difficulty=difficulty(kind),skill_ups=1,
          observed_skill=state.skill,observed_at=snapshot.observed_at}
        if complete and positive(item) and positive(qmin) and positive(qmax) and qmax>=qmin then
          recipe.item_id,recipe.qty_min,recipe.qty_max,recipe.inputs=item,qmin,qmax,inputs
        elseif state.recipes[tostring(spell)] then
          local previous=state.recipes[tostring(spell)]
          recipe.item_id,recipe.qty_min,recipe.qty_max,recipe.inputs=previous.item_id,previous.qty_min,previous.qty_max,previous.inputs
        end
        state.recipes[tostring(spell)]=recipe;count=count+1
      end
    end
    state.coverage="visible_recipes"
  elseif state and C_TradeSkillUI and C_TradeSkillUI.GetAllRecipeIDs and C_TradeSkillUI.GetRecipeInfo then
    if not (C_TradeSkillUI.IsDataSourceChanging and C_TradeSkillUI.IsDataSourceChanging()) then
      for _,id in ipairs(C_TradeSkillUI.GetAllRecipeIDs() or {}) do
        local info=C_TradeSkillUI.GetRecipeInfo(id)
        if info and info.learned and count<5000 then
          local observed=C:ObservedModernRecipe(id,info.name)
          observed.name=observed.name or ""
          observed.difficulty=difficulty(info.relativeDifficulty)
          observed.skill_ups=positive(info.numSkillUps) and math.min(10,info.numSkillUps) or 1
          observed.observed_skill,observed.observed_at=state.skill,snapshot.observed_at
          if not observed.item_id and state.recipes[tostring(id)] then
            local previous=state.recipes[tostring(id)]
            observed.item_id,observed.qty_min,observed.qty_max,observed.inputs=previous.item_id,previous.qty_min,previous.qty_max,previous.inputs
          end
          state.recipes[tostring(id)]=observed;count=count+1
        end
      end
      state.coverage="visible_recipes"
    end
  end
  C.db.profession_profiles[identity]=snapshot
  -- Use the existing production channel; older Workers already forward it.
  C.db.production=C.db.production or {events={},sequence=0,part_sequence=0,parcels={},sent={},received={},done={}}
  C.db.production.profession_snapshot=snapshot
  self.snapshot=snapshot
  if not silent then
    print("Cennik: профессии " .. character .. ": " .. (state and (state.name .. " " .. state.skill .. "/" .. state.max_skill .. ", прочитано рецептов: " .. count) or "уровни считаны; откройте профессию для рецептов") .. ". Для отправки выполните /reload и дождитесь Worker.")
    print("Cennik: скрытые фильтрами и свёрнутыми категориями рецепты могут отсутствовать. Ранее считанные рецепты сохранены.")
  end
  return snapshot
end
function P:CheckTask(task)
  if task.strategy_id~="profession_leveling" then return true end
  local contract=task.opportunity and task.opportunity.details and task.opportunity.details.leveling
  if task.format_version~=4 or not contract or contract.profile_id~=C.db.client_id then return false,"План прокачки предназначен другому персонажу или требует обновления аддона" end
  local snapshot=self:Capture(true)
  local recipe=task.opportunity.details.recipe
  local state=snapshot and snapshot.professions[key(recipe.profession)]
  if not state then
    for _,p in pairs(snapshot and snapshot.professions or {}) do if p.recipes[tostring(recipe.spell_id)] then state=p;break end end
  end
  if not state or state.skill~=contract.skill then return false,"Навык изменился или неизвестен. Откройте профессию, отправьте данные и обновите план прокачки в вебе" end
  return true
end
local frame=CreateFrame("Frame")
for _,event in ipairs({"ADDON_LOADED","TRADE_SKILL_SHOW","TRADE_SKILL_LIST_UPDATE","TRADE_SKILL_DATA_SOURCE_CHANGED","SKILL_LINES_CHANGED","PLAYER_ENTERING_WORLD"}) do pcall(frame.RegisterEvent,frame,event) end
frame:SetScript("OnEvent",function(_,event,addon)
  if event=="ADDON_LOADED" and addon~="CennikAddon" then return end
  if P.queued then return end
  P.queued=true
  C_Timer.After(0.5,function()
    P.queued=false
    local ok,error=pcall(P.Capture,P,true)
    if not ok then P.lastError=tostring(error);print("Cennik: не удалось считать профессию: " .. P.lastError) end
  end)
end)
