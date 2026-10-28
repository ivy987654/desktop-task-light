"""Generate a local view of real state-machine output using simulated events."""
import colorsys
import copy
import html
from pathlib import Path
import q5he_hid as hid
import statuslight as sl

cfg=copy.deepcopy(sl.DEFAULT_CONFIG)
cfg['proxy']['enabled']=False
cfg['codex']['process_check']=False
led_map=hid.STATIC_LED_MAP[0x0B50]
layout=sl.build_layout(led_map,cfg,101)
cards=[]
for kind,title,caption in [('thinking','运行中','任务进度沿列展开'),('waiting','等待确认','空格键显示等待状态'),('done','完成','短暂显示绿色结果'),('failed','中断','短暂显示红色结果')]:
    engine=sl.Engine(cfg)
    engine.handle({'type':'turn_start','session':'example','turn':'one','origin':'hook','ts':1000.0},1000.0)
    now=1060.0
    if kind=='waiting':engine.handle({'type':'wait','session':'example','kind':'approval','ts':now},now)
    elif kind in ('done','failed'):engine.handle({'type':'turn_complete' if kind=='done' else 'turn_abort','session':'example','turn':'one','ts':now},now)
    colors,region,region_color,_=engine.frame(now,layout,True)
    if kind in ('done','failed'):
        assert any(value==tuple(cfg['colors'][kind]) for value in colors.values())
    keys=[]
    for row in led_map:
        line=[]
        for led in row:
            if led==255:
                line.append('<i class="gap"></i>');continue
            hue,sat=colors.get(led,cfg['colors']['base'])
            if led in region and region_color:
                _,hue,sat=region_color
            rgb=colorsys.hsv_to_rgb(hue/255,sat/255,1)
            color='#'+''.join(f'{round(c*255):02x}' for c in rgb)
            line.append(f'<i style="--key:{color}"></i>')
        keys.append('<div class="row">'+''.join(line)+'</div>')
    cards.append(f'<section><div class="label"><h2>{html.escape(title)}</h2><span>{kind.upper()}</span></div><div class="board">'+''.join(keys)+f'</div><p>{html.escape(caption)}</p></section>')
page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>桌面任务状态灯 · 程序预览</title><style>
*{box-sizing:border-box}body{margin:0;background:#10151d;color:#e8edf4;font:15px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;padding:46px 56px}main{max-width:1250px;margin:auto}small{color:#91c5c3;letter-spacing:3px}h1{font-size:34px;margin:12px 0}header p{color:#9ba8ba;margin-bottom:30px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}section{background:#18212d;border:1px solid #2b3849;border-radius:16px;padding:24px}.label{display:flex;align-items:center;justify-content:space-between}h2{font-size:19px;margin:0 0 20px}.label span{color:#738698;font-size:11px;letter-spacing:2px}.board{background:#0b1018;border-radius:10px;padding:16px 12px}.row{display:grid;grid-template-columns:repeat(19,1fr);gap:4px;margin-bottom:4px}.row:last-child{margin:0}i{height:19px;border:1px solid color-mix(in srgb,var(--key) 60%,transparent);border-radius:3px;background:color-mix(in srgb,var(--key) 27%,#151c26);box-shadow:inset 0 -2px 0 #0005}.gap{visibility:hidden}section p{font-size:12px;color:#a2b2c4;margin:16px 0 0}footer{color:#738698;margin-top:26px;font-size:12px}
</style><main><header><small>DESKTOP TASK LIGHT</small><h1>抬眼看任务，低头看灯。</h1><p>运行、等待、完成与中断，用四种灯光说明当前状态。</p></header><div class="grid">'''+''.join(cards)+'''</div><footer>程序输出预览 · 输入为模拟事件 · 颜色由 statuslight.Engine 计算 · 此页只生成 HTML</footer></main></html>'''
Path(__file__).with_name('preview.html').write_text(page,encoding='utf-8')
print('Generated preview.html')
