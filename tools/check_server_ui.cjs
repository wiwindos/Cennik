"use strict";
const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),path=require('node:path');
(async()=>{
  const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('response',r=>{if(r.status()>=400&&!r.url().endsWith('/favicon.ico'))errors.push(r.status()+' '+r.url());});
  try{
    await page.goto('http://127.0.0.1:8772');
    await page.locator('#server-login [name="key"]').fill('ui-fixture-'+'x'.repeat(40));
    await page.locator('#server-login button').click();
    await page.getByRole('heading',{name:'Серверы и подключения',exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelector('#ahl-market-select')?.options.length===2);
    await page.locator('[data-edit-market]').first().click();
    await page.locator('#server-market [name="realm_names"]').fill('FixtureRealm');
    await page.getByRole('button',{name:'Сохранить рынок',exact:true}).click();
    await page.getByText('Рынок сохранён.',{exact:false}).waitFor();
    const pending=await page.evaluate(async()=>await(await fetch('/pair/v1/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:'Подключение по коду'})})).json());
    await page.locator(`.pair-approve[data-code="${pending.code}"]`).waitFor();
    await page.locator(`.pair-approve[data-code="${pending.code}"] button`).click();
    await page.getByText('ПК подтверждён.',{exact:false}).waitFor();
    const paired=await page.evaluate(async secret=>await(await fetch('/pair/v1/poll',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({secret})})).json(),pending.secret);
    assert.equal(paired.state,'approved');
    await page.locator('#server-worker [name="label"]').fill('Игровой ПК');
    await page.locator('#server-worker [name="clients"]').fill('auctioneer | FixturePlayer | FixtureRealm | first\ncrafter | Crafter | FixtureRealm | second');
    await page.getByRole('button',{name:'Выдать ключ Worker',exact:true}).click();
    await page.locator('#worker-invitation').filter({hasText:'Ключ'}).waitFor();
    await page.locator('#server-collector [name="label"]').fill('Сборщик');
    await page.getByRole('button',{name:'Выдать ключ Collector',exact:true}).click();
    await page.locator('#collector-invitation').filter({hasText:'Ключ'}).waitFor();
    // Hide one-time test credentials before screenshots.
    await page.evaluate(()=>{document.getElementById('worker-invitation').hidden=true;document.getElementById('collector-invitation').hidden=true;});
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1100});
      await page.screenshot({path:path.join(process.argv[4],`server-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Portal overflow '+width);
    }
    await page.getByRole('link',{name:'Открыть полный Cennik',exact:true}).click();
    await page.locator('nav [data-page="opportunities"]').waitFor();
    const prefix=new URL(page.url()).pathname.replace(/\/$/,'');
    const overview=await page.evaluate(async prefix=>await(await fetch(prefix+'/api/v2/overview')).json(),prefix);
    assert.equal(overview.server_mode,true);assert(overview.market.endsWith('.alliance.eu'));
    await page.waitForFunction(()=>window.CennikServerMode===true&&window.CennikMarket?.endsWith('.alliance.eu'));
    await page.waitForFunction(async prefix=>{const d=await(await fetch(prefix+'/api/v2/overview')).json();return !d.jobs.some(j=>['queued','running'].includes(j.state));},prefix);
    await page.locator('#own-data-mode').check();
    await page.getByText('Режим сохранён.',{exact:false}).waitFor();
    assert.equal(await page.evaluate(async prefix=>(await(await fetch(prefix+'/api/v2/settings')).json()).workspace.own_data_only,prefix),true);
    await page.locator('nav [data-page="sniper"]').click();
    await page.locator('#sniper-form [name="item_id"]').fill('2589');
    await page.locator('#sniper-form [name="quantity"]').fill('5');
    await page.getByRole('button',{name:'Сохранить цель',exact:true}).click();
    await page.getByText('Цель сохранена.',{exact:false}).waitFor();
    const sniper=await page.evaluate(async prefix=>await(await fetch(prefix+'/api/v2/sniper')).json(),prefix);
    assert.equal(sniper.rules[0].market,overview.market);
    for(const name of ['bulk','sniper','market','archive','execution','accounting','production','collectors','sources','settings']){
      await page.locator(`nav [data-page="${name}"]`).click();
      await page.waitForFunction(name=>document.querySelector(`nav [data-page="${name}"]`)?.getAttribute('aria-current')==='page',name);
      await page.waitForFunction(()=>!document.querySelector('#content')?.textContent.includes('Загрузка…'));
    }
    await page.locator('#ahl-token-form [name=token]').fill('ahlk_browser_fixture');
    await page.locator('#ahl-token-form button').first().click();
    await page.getByText('Настройки токена сохранены.',{exact:true}).waitFor();
    assert.equal(await page.locator('#ahl-token-form [name=token]').inputValue(),'');
    const saved=await page.evaluate(async prefix=>await(await fetch(prefix+'/api/v2/settings')).json(),prefix);
    assert.equal(saved.upstreams.ahl_token_set,true);assert(!JSON.stringify(saved).includes('ahlk_browser_fixture'));
    await page.locator('[data-ahl-check]').click();
    await page.getByText('Проверка соединения поставлена в очередь.',{exact:true}).waitFor();
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1100});
      await page.screenshot({path:path.join(process.argv[4],`release-settings-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Release settings overflow '+width);
    }
    await page.locator('[data-ahl-clear]').click();
    await page.getByText('Токен удалён.',{exact:true}).waitFor();
    await page.locator('nav [data-page="collectors"]').click();
    await page.getByRole('heading',{name:'Заявки аукционистов',exact:true}).waitFor();
    assert.equal(await page.locator('#collector-connection').isVisible(),false);
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1100});
      await page.screenshot({path:path.join(process.argv[4],`server-market-${width}.png`),fullPage:true});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Market overflow '+width);
    }
    assert.deepEqual(errors,[]);
    console.log('Server UI passed: owner login, realm binding, Worker/Collector keys, all 11 full-server sections, isolated EU market, 1440/390');
  }catch(error){await page.screenshot({path:path.join(process.argv[4],'failure.png'),fullPage:true});console.error('Browser errors:',errors);throw error;}
  finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
