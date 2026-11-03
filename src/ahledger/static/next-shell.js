"use strict";
(() => {
  const prefix=location.pathname.match(/^\/cennik(?:\/|$)/)?"/cennik":"";
  window.CennikHubPrefix=prefix;
  const original=window.fetch.bind(window);
  const identity=prefix?original('/auth/me').then(async r=>{if(!r.ok){location.assign('/login');throw Error('Требуется вход в WIW Hub');}return r.json();}):Promise.resolve(null);
  if(prefix)window.fetch=async(input,options={})=>{
    const url=new URL(typeof input==='string'?input:input.url,location.href);
    if(url.origin===location.origin&&url.pathname.startsWith('/cennik/')){
      const me=await identity,headers=new Headers(options.headers||{});
      headers.set('X-CSRF-Token',me.csrf);options={...options,headers};
    }
    const r=await original(input,options);
    if(r.status===401&&url.origin===location.origin)location.assign('/login');
    return r;
  };
  let prefs={};try{prefs=JSON.parse(localStorage.getItem('goldscope.preferences')||'{}');}catch{}
  const theme=()=>document.body.classList.toggle('light',prefs.theme==='light');theme();
  const tools=document.querySelector('.header-actions')||document.querySelector('header .actions')||document.querySelector('header');
  if(prefix){
    const link=document.createElement('a');link.href='/';link.className='hub-return';link.textContent='← WIW Hub';tools.prepend(link);
  }
  const button=document.createElement('button');button.type='button';button.className='theme-toggle';button.textContent='◐';button.setAttribute('aria-label','Переключить тему');
  button.onclick=()=>{prefs.theme=prefs.theme==='light'?'dark':'light';theme();try{localStorage.setItem('goldscope.preferences',JSON.stringify(prefs));}catch{}};tools.append(button);
})();
