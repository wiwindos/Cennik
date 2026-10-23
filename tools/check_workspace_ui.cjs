"use strict";
const {chromium} = require(process.argv[2]);
const assert = require("node:assert/strict");
const path = require("node:path");
(async () => {
  const browser = await chromium.launch({executablePath:process.argv[3],headless:true});
  const page = await browser.newPage({viewport:{width:1440,height:1000},colorScheme:"light"});
  const errors=[];
  page.on("pageerror",error=>errors.push(String(error)));
  page.on("response",response=>{if(response.status()>=400)errors.push(response.status()+" "+response.url());});
  const navigate=async name=>{await page.locator(`nav [data-page="${name}"]`).click();await page.waitForFunction(()=>document.querySelector('h1')?.textContent!=="Cennik");};
  try {
    await page.goto("http://127.0.0.1:8767");
    await page.locator("[data-opportunity]").first().click();
    await page.locator("#batches").fill("2");
    await page.locator('[data-action="plan"]').click();
    await page.getByText("Книга предложений:",{exact:false}).waitFor();
    assert.match(await page.locator(".detail").innerText(),/20 м/);
    await page.screenshot({path:path.join(process.argv[4],"opportunities.png"),fullPage:true});
    await navigate("archive");
    await page.locator("[data-scan]").first().click();
    await page.getByRole("button",{name:"Следующие строки"}).click();
    await page.getByText("101–120 из 120",{exact:false}).waitFor();
    await navigate("market");
    await page.locator('[name="q"]').fill("2589");
    await page.getByRole("button",{name:"Найти",exact:true}).click();
    await page.locator("[data-item-view]").first().click();
    await page.locator("#facts-form").waitFor();
    await page.locator('[data-snipe-item="2589"]').first().click();
    await page.locator('#sniper-form').waitFor();
    assert.equal(await page.locator('#sniper-form [name="item_id"]').inputValue(),"2589");
    await page.locator('#sniper-form [name="quantity"]').fill("25");
    await page.locator('#sniper-form [name="mode"]').selectOption("limit");
    await page.locator('#sniper-form [name="price_gold"]').fill("0.125");
    await page.getByRole('button',{name:'Сохранить цель',exact:true}).click();
    await page.getByText('Цель сохранена.',{exact:false}).waitFor({timeout:5000}).catch(async e=>{throw Error((await page.locator('#message').innerText())+' '+errors.join('; ')+' '+e);});
    const rules=await page.evaluate(async()=>await (await fetch('/api/v2/sniper')).json());
    assert.equal(rules.rules.length,1);assert.equal(rules.rules[0].unit_limit_copper,1250);assert.equal(rules.rules[0].quantity,25);
    await page.getByRole('button',{name:'Изменить',exact:true}).click();
    await page.locator('#sniper-form [name="quantity"]').fill("40");
    await page.getByRole('button',{name:'Сохранить цель',exact:true}).click();
    await page.getByRole('cell',{name:'40 шт.',exact:false}).waitFor();
    await page.getByText('Цели · 1',{exact:true}).waitFor();
    const portable=await page.evaluate(async()=>(await (await fetch('/api/v2/sniper/export')).json()).text);
    await page.locator('#sniper-import').fill(portable);
    await page.getByRole('button',{name:'Проверить импорт',exact:true}).click();
    await page.getByText('Добавить: 0; обновить: 1',{exact:false}).waitFor({timeout:5000}).catch(async e=>{throw Error((await page.locator('#message').innerText())+' '+errors.join('; ')+' '+e);});
    await page.getByRole('button',{name:'Применить',exact:true}).click();
    await page.getByText('Список объединён.',{exact:false}).waitFor();
    assert.equal(await page.locator('#sniper-budget').count(),0);
    await page.getByRole('button',{name:'Рецепт / материалы',exact:true}).click();
    await page.getByRole('button',{name:'Снайпить все материалы',exact:true}).click();
    const dialog=page.locator('#sniper-material-dialog');await dialog.waitFor();
    await dialog.locator('[name="batches"]').fill('4');
    assert.ok(await dialog.locator('[data-apply]').isDisabled());
    await dialog.getByRole('button',{name:'Проверить состав',exact:true}).click();
    await dialog.getByText('Fixture cloth × 12',{exact:false}).waitFor();
    await dialog.getByText('Fixture thread × 4 · NPC: отдельно',{exact:false}).waitFor();
    await dialog.getByRole('button',{name:'Добавить материалы',exact:true}).click();
    await page.getByText('Цели · 2',{exact:true}).waitFor();
    await page.screenshot({path:path.join(process.argv[4],"sniper.png"),fullPage:true});
    for(const name of ["execution","sources","settings"]) await navigate(name);
    await page.locator("#settings-form").waitFor();
    const schemas=await page.evaluate(async()=>await (await fetch('/api/v2/settings')).json());
    for(const scope of ['analytics','workspace','graph']) {
      const expected=Object.keys(schemas.schemas[scope==='graph'?'craft_graph':scope].properties)
        .filter(key=>!['enabled_strategies','strategy_settings'].includes(key)).sort();
      const actual=await page.locator(`[data-scope="${scope}"]`).evaluateAll(inputs=>inputs.map(i=>i.dataset.setting).sort());
      assert.deepEqual(actual,expected,`Missing ${scope} settings`);
    }
    assert.equal(await page.locator('[data-scope="graph"]').count(),3);
    await page.getByRole("button",{name:"Сохранить настройки",exact:true}).click();
    await page.getByText("Настройки сохранены.",{exact:false}).waitFor();
    await page.screenshot({path:path.join(process.argv[4],"settings.png"),fullPage:true});
    for(const width of [320,736,1024]) {
      await page.setViewportSize({width,height:900});
      for(const name of ["opportunities","market","archive","execution","sources","settings","sniper"]) {
        await navigate(name);
        await page.locator('nav [aria-current="page"][data-page="'+name+'"]').waitFor();
        const dimensions=await page.evaluate(()=>({body:document.documentElement.scrollWidth,width:innerWidth}));
        assert.ok(dimensions.body<=dimensions.width+1,`${name} overflows at ${width}: ${JSON.stringify(dimensions)}`);
      }
    }
    assert.deepEqual(errors,[]);
    process.stdout.write(JSON.stringify({passed:true,screens:7,widths:[320,736,1024,1440],errors},null,2)+"\n");
  } finally {await browser.close();}
})().catch(error=>{process.stderr.write(String(error.stack)+"\n");process.exitCode=1;});
