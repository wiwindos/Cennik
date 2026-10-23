local _, C = ...
local S,U=C.Sniper,C.UI
local panel,text,button,input=S.MakePanel,S.MakeText,S.MakeButton,S.MakeInput
local professions={Tailoring="Портняжное дело",Engineering="Инженерное дело",Alchemy="Алхимия",Blacksmithing="Кузнечное дело",Leatherworking="Кожевничество",Cooking="Кулинария",Enchanting="Наложение чар"}
local function itemText(parent,item,suffix,x,y,width)
  local label=text(parent,C.Items:Label(item) .. (suffix or ""),x,y,width)
  C.Items:Load(item,function(info) if info then label:SetText(C.Items:Label(item) .. (suffix or "")) end end)
  return label
end
function S:ShowPicker(rows,callback)
  if #rows==1 then callback(rows[1].item_id); return end
  local p=panel(540,390)
  text(p,#rows>0 and "Выберите предмет" or "Не найдено. Попробуйте другое название или ID.",18,-18,490)
  local scroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate"); scroll:SetPoint("TOPLEFT",18,-50); scroll:SetSize(485,275); scroll:SetClipsChildren(true)
  local body=CreateFrame("Frame",nil,scroll); body:SetSize(470,math.max(275,#rows*30)); scroll:SetScrollChild(body)
  for i,row in ipairs(rows) do button(body,C.Items:Label(row.item_id) .. " (#" .. row.item_id .. ")",0,-30*(i-1),465,function() p:Hide(); callback(row.item_id) end) end
  button(p,"Закрыть",365,-345,145,function() p:Hide() end)
end
function S:ShowCard(item,index)
  if self.card then self.card:Hide() end
  local p=panel(590,505); self.card=p
  itemText(p,item," (#" .. item .. ")",18,-18,545)
  button(p,"Снайпить предмет",18,-48,225,function() self:ShowEditor(item) end)
  local recipes=self:Recipes(item); index=index or 1; local recipe=recipes[index]
  if not recipe then
    text(p,"Состав рецепта отсутствует в переданном каталоге.\nНажмите «Запросить рецепты», выполните /reload, дождитесь\nсинхронизации в вебе, затем ещё раз /reload.\nТакже можно обновить рецепты в вебе: «Источники».",18,-95,545)
    button(p,"Запросить рецепты",18,-185,225,function()
      if #C.db.sniper.events>=1900 then self:Notice("Сначала синхронизируйте журнал"); return end
      self:Emit("recipe_request",{item_id=item}); self:Notice("Запрос записан. /reload → дождитесь веба → /reload"); p:Hide()
    end)
  else
    local profession=professions[recipe.profession] or recipe.profession or "Профессия неизвестна"
    text(p,"Рецепт " .. index .. "/" .. #recipes .. " · " .. profession .. "; навык " .. tostring(recipe.skill_required or "?") ..
      "\nВыход за крафт: " .. tostring(recipe.qty_min or "?") .. (recipe.qty_max~=recipe.qty_min and ("–" .. tostring(recipe.qty_max or "?")) or ""),18,-88,545)
    if #recipes>1 then button(p,"Другой рецепт",350,-48,210,function() self:ShowCard(item,index%#recipes+1) end) end
    text(p,"Число крафтов",18,-135,180); local batches=input(p,1,175,-128,100)
    button(p,"Снайпить все материалы",292,-128,268,function()
      local n=tonumber(batches:GetText())
      if not C:Integer(n) or n>10000 then self:Notice("Число крафтов: от 1 до 10000"); return end
      self:ShowMaterialPreview(recipe,n)
    end)
    local scroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate"); scroll:SetPoint("TOPLEFT",18,-170); scroll:SetSize(525,250); scroll:SetClipsChildren(true)
    local body=CreateFrame("Frame",nil,scroll); body:SetSize(510,math.max(250,#recipe.materials*47)); scroll:SetScrollChild(body)
    for i,line in ipairs(recipe.materials) do
      local facts=self:CatalogItem(line.item_id); local npc=facts and facts.vendor_buy_copper and facts.vendor_buy_copper>0
      itemText(body,line.item_id," × " .. line.quantity .. (npc and " · есть цена NPC" or " · аукцион"),0,-47*(i-1),335)
      button(body,"В снайпер",355,-47*(i-1),145,function()
        local n=tonumber(batches:GetText())
        if C:Integer(n) and n<=10000 then self:ShowEditor(line.item_id,nil,{item_id=line.item_id,quantity=line.quantity*n,mode="any",priority="normal",discount_bps=5000,unit_limit_copper=0,use_bags=false}) end
      end)
    end
    text(p,"Показаны непосредственные материалы. Для NPC проверьте цену и доступ.\nЗолото персонажа не ограничивает поиск на аукционе.",18,-430,545)
  end
  button(p,"Закрыть",410,-470,150,function() p:Hide() end)
end
function S:ShowMaterialPreview(recipe,batches)
  local p=panel(565,470); self.materialPreview=p
  text(p,"Материалы · " .. batches .. " крафтов · " .. C.Items:Name(recipe.item_id),18,-18,520)
  local lines,auction={},{}
  for _,line in ipairs(recipe.materials) do
    local n=line.quantity*batches
    if not C:Integer(n) or n>1000000 then self:Notice("Количество материала превышает 1000000"); p:Hide(); return end
    local facts=self:CatalogItem(line.item_id); local npc=facts and facts.vendor_buy_copper and facts.vendor_buy_copper>0
    lines[#lines+1]=C.Items:Label(line.item_id) .. " × " .. n .. (npc and " · NPC: отдельно" or " · добавить в снайпер")
    if not npc then auction[#auction+1]={item_id=line.item_id,quantity=n} end
  end
  local scroll=CreateFrame("ScrollFrame",nil,p,"UIPanelScrollFrameTemplate"); scroll:SetPoint("TOPLEFT",18,-50); scroll:SetSize(510,230); scroll:SetClipsChildren(true)
  local body=CreateFrame("Frame",nil,scroll); body:SetSize(495,230); scroll:SetScrollChild(body)
  local label=text(body,table.concat(lines,"\n\n"),0,0,490); body:SetHeight(math.max(230,label:GetStringHeight()+20))
  local bags=CreateFrame("CheckButton",nil,p,"UICheckButtonTemplate"); bags:SetPoint("TOPLEFT",15,-292); bags:SetChecked(false)
  text(p,"Вычесть сумки один раз при добавлении",48,-300,490)
  text(p,"Режим по умолчанию: «Любая цена». После добавления можно изменить\nусловие каждого материала. Новые цели приостановлены.",18,-338,520)
  local note=text(p,"",18,-383,520)
  button(p,"Добавить материалы",18,-420,245,function()
    if #auction==0 then note:SetText("Материалов для поиска на аукционе нет"); return end
    local definitions={}
    for _,line in ipairs(auction) do definitions[#definitions+1]=S:NewDefinition(line.item_id,line.quantity,bags:GetChecked()) end
    local ok,reason=S:Apply({budget=C.MAX_INTEGER,rules=definitions})
    if not ok then note:SetText(reason); return end
    p:Hide(); if S.card then S.card:Hide() end; U:Show(); U:SelectSection("Sniper")
  end)
  button(p,"Закрыть",380,-420,150,function() p:Hide() end)
end
function S:ShowManualBought(identity)
  local rule=C.db.sniper.rules[identity]; if not rule then return end
  local p=panel(485,230)
  text(p,"Учесть покупку другим персонажем / вне Cennik\nУказывайте фактически купленное. Осталось: " .. self:Remaining(rule),18,-18,445)
  local quantity=input(p,self:Remaining(rule),24,-80,180)
  local note=text(p,"Стоимость неизвестна и не будет придумана.",18,-120,445)
  button(p,"Учесть количество",18,-173,240,function()
    local ok,reason=S:MarkBought(identity,tonumber(quantity:GetText()))
    if ok then p:Hide(); C:Refresh() else note:SetText(reason) end
  end)
  button(p,"Закрыть",310,-173,150,function() p:Hide() end)
end
function S:ShowAuctionatorShortcut(item)
  local p=self.auctionatorShortcut
  if not p then
    p=panel(315,100); self.auctionatorShortcut=p; p:ClearAllPoints(); p:SetPoint("BOTTOMRIGHT",AuctionHouseFrame,"BOTTOMRIGHT",-15,45)
    p.item=text(p,"",12,-12,290)
    button(p,"В снайпер",12,-39,140,function() S:ShowEditor(S.auctionatorSelection.itemID) end)
    button(p,"Предмет / рецепт",160,-39,145,function() S:ShowCard(S.auctionatorSelection.itemID) end)
    p:SetScript("OnUpdate",function(self) if not C.Adapter:Open() then self:Hide() end end)
  end
  p.item:SetText(C.Items:Label(item)); p:Show()
end
function U:HiddenTasks()
  local parent=self.body
  local back=self:Widget("Button",parent,"UIPanelButtonTemplate","hidden"); back:SetSize(180,25); back:SetPoint("TOPLEFT",0,0); back:SetText("К возможностям"); back:SetScript("OnClick",function() U:SelectSection("Opportunities") end)
  local scroll=self:Widget("ScrollFrame",parent,"UIPanelScrollFrameTemplate","hidden"); scroll:SetPoint("TOPLEFT",0,-40); scroll:SetSize(625,math.max(80,parent:GetHeight()-48)); scroll:SetClipsChildren(true)
  local body=self:Widget("Frame",scroll,nil,"hidden-body"); local tasks={}
  for _,task in pairs(C.db.tasks) do if task.hidden then tasks[#tasks+1]=task end end
  table.sort(tasks,function(a,b) return a.task_id<b.task_id end)
  for i,task in ipairs(tasks) do
    local label=self:Widget("FontString",body,"GameFontHighlightSmall","hidden-label"); label:SetPoint("TOPLEFT",0,-40*(i-1)); label:SetWidth(315); label:SetJustifyH("LEFT"); label:SetText(C.Items:Label(task.item_id))
    local function action(caption,x,callback)
      local b=self:Widget("Button",body,"UIPanelButtonTemplate","hidden-button"); b:Enable(); b:SetPoint("TOPLEFT",x,-40*(i-1)); b:SetSize(130,25); b:SetText(caption); b:SetScript("OnClick",callback)
    end
    action("Вернуть",325,function()
      if task.run_id~=C.db.current_run or task.market~=C.db.current_market then U.notice="Это задание старого расчёта. Создайте новое предложение в вебе; старое можно удалить."; C:Refresh(); return end
      task.hidden=nil; task.validation=nil; C:State(task,"pending_scan","Задание возвращено; повторите проверку"); U:SelectSection("Opportunities")
    end)
    action("Удалить",465,function()
      local p=panel(445,170); text(p,"Удалить задание из скрытых?\nИстория покупок сохранится. Повторный импорт того же задания\nне восстановит его. Новые предложения предмета разрешены.",18,-18,405)
      button(p,"Удалить",18,-115,180,function() local ok,reason=C:DeleteHiddenTask(task); if ok then p:Hide(); C:Refresh() else C.UI.notice=reason; C:Refresh() end end)
      button(p,"Отмена",235,-115,180,function() p:Hide() end)
    end)
  end
  if #tasks==0 then local label=self:Widget("FontString",body,"GameFontHighlightSmall","hidden-label"); label:SetPoint("TOPLEFT",0,0); label:SetText("Скрытых заданий нет") end
  body:SetSize(605,math.max(scroll:GetHeight(),#tasks*40)); self:AttachScrollChild(scroll,body)
end
