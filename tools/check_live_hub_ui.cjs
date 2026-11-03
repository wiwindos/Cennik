const {chromium}=require(process.argv[2]);
const assert=require('node:assert/strict'),path=require('node:path'),fs=require('node:fs');
(async()=>{
 const out=process.argv[4],origin='https://205.172.56.135:8444',profile='91b1ea41e8235e73b823d87a6065b75c';
 fs.mkdirSync(out,{recursive:true});
 const session=JSON.parse(fs.readFileSync(process.argv[5],'utf8'));
 const browser=await chromium.launch({executablePath:process.argv[3],headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1050}});
 await context.addCookies([{name:'market_session',value:session.cookie,domain:'205.172.56.135',path:'/',secure:true,httpOnly:true,sameSite:'Strict',expires:session.expires}]);
 const page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(String(e)));
 page.on('response',r=>{if(r.status()>=400)errors.push(r.status()+' '+r.url());});
 try{
  await page.goto(origin+'/');
  await page.locator('.module-card').filter({hasText:'Cennik'}).waitFor();
  assert(await page.locator('.module-card').filter({hasText:'Goldscope'}).count());
  await page.screenshot({path:path.join(out,'hub.png'),fullPage:true});
  await page.locator('.module-card').filter({hasText:'Cennik'}).click();
  await page.getByRole('heading',{name:'Серверы и подключения',exact:true}).waitFor();
  await page.locator('a[href="/cennik/m/'+profile+'/"]').click();
  await page.getByRole('heading',{name:'Возможности',exact:true}).waitFor();
  await page.locator('[data-opportunity]').first().waitFor();
  const pages=['opportunities','bulk','sniper','market','archive','execution','accounting','production','leveling','collectors','sources','settings'];
  for(const name of pages){
   await page.locator('nav [data-page="'+name+'"]').click();
   await page.waitForFunction(n=>document.querySelector('nav [data-page="'+n+'"]')?.hasAttribute('aria-current'),name);
   await page.locator('#content h1').first().waitFor();
   await page.waitForTimeout(150);
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'overflow '+name);
  }
  for(const width of [1440,390]){
   await page.setViewportSize({width,height:1050});
   await page.locator('nav [data-page="sources"]').click();await page.locator('#upstream-settings').waitFor();
   await page.screenshot({path:path.join(out,'sources-'+width+'.png'),fullPage:true});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  }
  assert.deepEqual(errors,[]);
  const result={public_https:true,hub_session_no_second_login:true,working_profile:profile,pages,js_http_errors:errors};
  fs.writeFileSync(path.join(out,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 }catch(e){await page.screenshot({path:path.join(out,'failure.png'),fullPage:true});console.error(errors);throw e;}
 finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
