file_path = r'C:\Users\33082\PycharmProjects\wellflow-saas-backend\static\wellflow-html\04-投放台.html'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 修改素材卡片，加上驳回原因显示
old_card = '''    return '<div class="m" style="cursor:pointer" onclick="openDetail(\''+goto+'\')">'+
      '<div class="cover">'+cover+
        '<div class="chk" onclick="event.stopPropagation();this.classList.toggle(\'on\');this.closest(\'.m\').classList.toggle(\'sel\');updateFloatBar()"></div>'+
        '<span class="tag-l">'+(m.type==='video'?'视频':'图片')+'</span>'+
        '<span class="tag-r '+stCls(st)+'">'+st+'</span>'+
      '</div>'+
      '<div class="data">'+
        '<span>成交 <b>—</b></span><span>点击率 <b>—</b></span>'+
        '<span>转化率 <b>—</b></span><span>ROI <b>—</b></span>'+
      '</div>'+
      '<div class="info">'+
        '<div class="ttl">'+fn+'</div>'+
        '<div class="meta">'+(info.plan_name||'本地上传')+' · '+sz+'</div>'+
        '<div class="tags"><span class="hi">'+st+'</span></div>'+
      '</div>'+
      '<div class="foot"><span class="spend">消耗 ¥—</span><span>曝光 —</span></div>'+
      '<div class="foot" style="border-top:none;padding-top:2px"><span>本地上传</span><span>'+time+'</span></div>'+
    '</div>';'''

new_card = '''    const rejectReason = m.reject_reason || info.reject_reason || '';
    const reasonHtml = rejectReason 
      ? '<div style="padding:6px 10px;background:rgba(234,102,104,0.1);border-top:1px solid rgba(234,102,104,0.2);font-size:11px;color:#EA6668;line-height:1.4" title="'+rejectReason+'">⚠️ '+rejectReason.substring(0,50)+(rejectReason.length>50?'...':'')+'</div>'
      : '';
    return '<div class="m" style="cursor:pointer" onclick="openDetail(\''+goto+'\')">'+
      '<div class="cover">'+cover+
        '<div class="chk" onclick="event.stopPropagation();this.classList.toggle(\'on\');this.closest(\'.m\').classList.toggle(\'sel\');updateFloatBar()"></div>'+
        '<span class="tag-l">'+(m.type==='video'?'视频':'图片')+'</span>'+
        '<span class="tag-r '+stCls(st)+'">'+st+'</span>'+
      '</div>'+
      '<div class="data">'+
        '<span>成交 <b>—</b></span><span>点击率 <b>—</b></span>'+
        '<span>转化率 <b>—</b></span><span>ROI <b>—</b></span>'+
      '</div>'+
      '<div class="info">'+
        '<div class="ttl">'+fn+'</div>'+
        '<div class="meta">'+(info.plan_name||'本地上传')+' · '+sz+'</div>'+
        '<div class="tags"><span class="hi">'+st+'</span></div>'+
      '</div>'+
      reasonHtml +
      '<div class="foot"><span class="spend">消耗 ¥—</span><span>曝光 —</span></div>'+
      '<div class="foot" style="border-top:none;padding-top:2px"><span>本地上传</span><span>'+time+'</span></div>'+
    '</div>';'''

content = content.replace(old_card, new_card)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print('✅ 已在素材卡片上加上驳回原因显示')
