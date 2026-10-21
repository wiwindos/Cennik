"use strict";
const byId = id => document.getElementById(id);
const datasetNames = {history:"История цен",pricetable:"Текущие цены",item:"Карточки предметов",crafting:"Рецепты",disenchant:"Распыление",structure:"Структура рынка",signals:"Сигналы",deals:"Предложения",economy_market:"Экономика",builds:"Изменения билдов",documentation:"Документация",llms:"Справочник"};
const stateNames = {idle:"Ожидание",running:"Загрузка",building:"Построение базы",stopping:"Остановка",stopped:"Остановлено",completed:"Завершено",partial:"Завершено с пропусками",failed:"Ошибка",interrupted:"Прервано перезапуском"};
let secret = sessionStorage.getItem("ahl-key") || "";
let localMode = false;
let timer = null, marketSignature = "", polling = false;
let updateStateInitialized = false, lastCompletedUpdate = null;
const number = value => Number(value || 0).toLocaleString("ru-RU");
function notice(text) { byId("message").textContent = text; byId("message").hidden = !text; }
async function api(path, options = {}) {
  const response = await fetch("/api" + path, {cache:"no-store", ...options, headers:{...(localMode ? {} : {"X-API-Key":secret}),"Content-Type":"application/json",...options.headers}});
  let value; try {value = await response.json();} catch {throw new Error("Сервер временно недоступен");}
  if (!response.ok) {
    if (response.status === 401) disconnect();
    const error = Array.isArray(value.detail) ? value.detail.map(v=>v.msg).join("; ") : value.detail;
    throw new Error(error || "Ошибка запроса");
  }
  return value;
}
function disconnect(){window.dispatchEvent(new Event("cennik-disconnected"));secret="";sessionStorage.removeItem("ahl-key");clearInterval(timer);timer=null;byId("login").hidden=false;byId("dashboard").hidden=true;byId("logout").hidden=true;byId("connection").textContent="Нет подключения";byId("connection").classList.remove("active");}
function setMarkets(markets){
  const signature = JSON.stringify(markets.map(m=>m.id)); if(signature===marketSignature)return;marketSignature=signature;
  const selected = new Set(Array.from(byId("markets").selectedOptions, o=>o.value));
  for(const id of ["markets","history-market"]){
    const previous=byId(id).value;byId(id).replaceChildren();
    for(const market of markets){const option=document.createElement("option");option.value=market.id;option.textContent=market.label||market.id;option.selected=id==="markets"?(selected.size?selected.has(market.id):market.id==="forever.pvp.horde.us"):market.id===(previous||"forever.pvp.horde.us");byId(id).append(option);}
  }
}
function render(value){
  setMarkets(value.markets);
  const active=["running","building","stopping"].includes(value.run.state);
  byId("run-state").textContent=value.run.state==="building"&&value.run.action==="update"?"Применение обновления":stateNames[value.run.state]||value.run.state;byId("run-state").classList.toggle("active",active);
  byId("parquet-note").textContent=value.parquet?.stale?"Parquet устарел после обновления базы. При необходимости нажмите «Экспортировать Parquet». Аналитика использует текущую DuckDB.":value.parquet?.ready?"Экспорт Parquet соответствует текущей базе.":"Parquet можно создать отдельной кнопкой. Обновление рынка не запускает полный экспорт.";
  byId("done").textContent=number(value.done);byId("remaining").textContent=number(value.remaining);byId("records").textContent=number(value.records);byId("raw").textContent=(value.raw_bytes/1048576).toFixed(1)+" МБ";
  byId("progress-fill").style.width=(value.total?Math.min(100,value.done/value.total*100):0)+"%";
  byId("progress-note").textContent=value.total?`${number(value.done)} из ${number(value.total)} заданий в выбранной очереди. Очередь может расти.`:"Очередь появится после первого запуска.";
  byId("errors").textContent=number(value.jobs.FAILED)+" / "+number(value.jobs.RETRY);
  byId("current").textContent=value.current.map(j=>[j.dataset,j.market,j.item_id].filter(Boolean).join(" · ")).join("; ")||(active?"Подготовка / обработка":"—");
  byId("eta").textContent=value.jobs_per_second?`${value.jobs_per_second.toFixed(2)} заданий/сек`+(value.eta_seconds!==null?` · примерно ${(value.eta_seconds/3600).toFixed(1)} ч`:""):"Пока неизвестно";
  byId("database").textContent=value.analytics_ready?"Готова"+(value.analytics_updated_at?" · "+new Date(value.analytics_updated_at).toLocaleString("ru-RU"):""):"Ещё не построена";
  byId("start").disabled=active||Boolean(value.imported_snapshot);byId("stop").disabled=!active||value.run.state==="stopping";
  byId("update-market").disabled=active||!value.market_update_available;
  document.querySelectorAll("[data-action]").forEach(button=>button.disabled=active||(button.dataset.action==="build"&&Boolean(value.imported_snapshot)));
  if(value.imported_snapshot)notice("Подключена серверная DuckDB. Используйте обновление текущей базы: оно сохраняет историю. Пересборка из прежнего локального raw-архива отключена.");
  if(value.run.action==="update"&&value.run.state==="completed"){
    notice("Данные PvP Alliance US обновлены. История цен сохранена; нажмите «Пересчитать аналитику» для новых результатов.");
    if(updateStateInitialized&&lastCompletedUpdate!==value.run.update_id)window.dispatchEvent(new Event("cennik-data-updated"));
    lastCompletedUpdate=value.run.update_id;
  }
  updateStateInitialized=true;
  if(value.run.error)notice(value.run.error);
}
async function poll(){if(polling||(!localMode&&!secret))return;polling=true;try{render(await api("/status"));byId("connection").textContent=localMode?"Локально":"Подключено";byId("connection").classList.add("active");}catch(error){notice(error.message);byId("connection").textContent="Нет связи";}finally{polling=false;}}
async function connect(){const value=await api("/status");if(!localMode)sessionStorage.setItem("ahl-key",secret);byId("key").value="";byId("login").hidden=true;byId("dashboard").hidden=false;byId("logout").hidden=localMode;render(value);await poll();clearInterval(timer);timer=setInterval(poll,2000);window.dispatchEvent(new Event("cennik-connected"));}
byId("login-form").addEventListener("submit",async event=>{event.preventDefault();secret=byId("key").value;try{await connect();byId("login-error").textContent="";}catch(error){byId("login-error").textContent=error.message;}});
byId("logout").addEventListener("click",disconnect);
for(const [name,label] of Object.entries(datasetNames)){const wrapper=document.createElement("label");wrapper.className="check";const input=document.createElement("input");input.type="checkbox";input.value=name;input.checked=true;input.disabled=true;wrapper.append(input,document.createTextNode(label));byId("datasets").append(wrapper);}
byId("all-datasets").addEventListener("change",event=>{byId("datasets").hidden=event.target.checked;document.querySelectorAll("#datasets input").forEach(input=>{input.disabled=event.target.checked;if(event.target.checked)input.checked=true;});});
function request(action){const datasets=byId("all-datasets").checked?[]:Array.from(document.querySelectorAll("#datasets input:checked"),input=>input.value);if(action==="download"&&!byId("all-datasets").checked&&!datasets.length)throw new Error("Выберите хотя бы один тип данных");const markets=Array.from(byId("markets").selectedOptions,option=>option.value);if(action==="download"&&!markets.length)throw new Error("Выберите хотя бы один рынок");return {action,markets,datasets,requests_per_second:Number(byId("rate").value),tools_per_second:Number(byId("tool-rate").value),retry_failed:byId("retry").checked};}
async function start(action){try{notice("");await api("/jobs",{method:"POST",body:JSON.stringify(request(action))});await poll();}catch(error){notice(error.message);}}
byId("download-form").addEventListener("submit",event=>{event.preventDefault();start("download");});
document.querySelectorAll("[data-action]").forEach(button=>button.addEventListener("click",()=>start(button.dataset.action)));
byId("update-market").addEventListener("click",async()=>{
  try{
    byId("update-market").disabled=true;notice("");
    await api("/jobs",{method:"POST",body:JSON.stringify({action:"update",markets:["forever.pvp.alliance.us"],
      requests_per_second:Number(byId("rate").value),tools_per_second:Number(byId("tool-rate").value),retry_failed:true})});
    await poll();
  }catch(error){notice(error.message);byId("update-market").disabled=false;}
});
byId("stop").addEventListener("click",async()=>{try{await api("/jobs/stop",{method:"POST"});await poll();}catch(error){notice(error.message);}});
byId("refresh-log").addEventListener("click",async()=>{try{byId("log").textContent=(await api("/logs")).text;byId("log").scrollTop=byId("log").scrollHeight;}catch(error){notice(error.message);}});
byId("search-form").addEventListener("submit",async event=>{event.preventDefault();try{const result=await api("/items?q="+encodeURIComponent(byId("search").value)+"&limit=30");byId("search-results").replaceChildren();for(const item of result.items){const button=document.createElement("button");button.type="button";button.textContent=`${item.id} · ${item.name||"Без названия"}`;button.addEventListener("click",()=>{byId("item-id").value=item.id;byId("history-form").requestSubmit();});byId("search-results").append(button);}if(!result.items.length)byId("search-results").textContent="Предметы не найдены; каталог может быть ещё не загружен.";}catch(error){notice(error.message);}});
byId("history-form").addEventListener("submit",async event=>{event.preventDefault();try{const market=byId("history-market").value;const item=Number(byId("item-id").value);const result=await api(`/prices/${encodeURIComponent(market)}/${item}?granularity=${byId("granularity").value}&limit=500`);byId("history").replaceChildren();for(const point of result.points){const row=document.createElement("tr");for(const value of [new Date(point.observed_at).toISOString().replace("T"," ").replace(".000Z",""),point.min_copper,point.median_copper,point.quantity,point.auctions]){const cell=document.createElement("td");cell.textContent=value===null?"—":String(value);row.append(cell);}byId("history").append(row);}byId("history-note").textContent=result.points.length?`${result.points.length} последних точек · ${result.market} · цены в меди`:"Для этого предмета и рынка сохранённых точек пока нет.";}catch(error){byId("history-note").textContent=error.message;}});
async function initialize(){
  try{
    const response=await fetch("/healthz",{cache:"no-store"});if(!response.ok)throw new Error("Приложение недоступно");
    localMode=(await response.json()).local_mode===true;
    if(localMode){document.querySelector("footer span").textContent="Локальный режим · доступ только с этой машины · ключ не требуется";}
    if(localMode||secret)await connect();
  }catch(error){byId("login-error").textContent=error.message;}
}
initialize();
