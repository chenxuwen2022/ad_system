# -*- coding: utf-8 -*-
import io

p = "routes/upload_routes.py"
t = io.open(p, encoding="utf-8").read()

old = """// 把 AI 文本解析为可视化元素：结论章节→彩色标签流，建议章节→卡片网格
function renderAiVisual(text){
    const raw = String(text==null?"":text).trim();
    if(!raw) return "<div class='muted'>（无返回）</div>";
    const lines = raw.split(/\\r?\\n/).map(l=>l.trim()).filter(l=>l.length>0);
    if(!lines.length) return "<div class='muted'>（无返回）</div>";
    const secs = [];
    let cur = null;
    lines.forEach(line=>{
        const m = line.match(/^(第?[一二三四五六七八九十百]+)[、.．:：)）]\\s*(.+)$/);
        if(m){
            let title = m[2].trim();
            // 长标题拆分：冒号前作为章节标签，冒号后内容作为要点 chip
            const cm = title.match(/^([^\\uFF1A:]{1,10})[\\uFF1A:]\\s*(.+)$/);
            if(cm){ title = cm[1].trim(); }
            else{ title = title.replace(/[\\uFF1A:]\\s*$/, ""); }
            cur = {title: title, items: []};
            secs.push(cur);
            if(cm && cm[2].trim()){ cur.items.push(cm[2].trim()); }
        }else{
            if(!cur){ cur = {title: "AI 结论", items: []}; secs.push(cur); }
            cur.items.push(line);
        }
    });
    if(!secs.length){ secs.push({title: "AI 结论", items: lines}); }
    let html = "";
    secs.forEach(sec=>{
        // 仅渲染"建议/优化/修改"类章节为卡片；诊断结论已由六维数据面板可视化，跳过文字
        if(!/建议|优化|修改|改法|方向|行动|提升|加强|关注/.test(sec.title)){ return; }
        html += `<div class="viz-sec"><span class="viz-sec-tag">${esc(sec.title)}</span></div>`;
        // 提取条目
        const cards = [];
        sec.items.forEach(line=>{
            const num = line.match(/^(\\d+)[、.．:：)）]\\s*(.+)$/);
            const circle = line.match(/^([①②③④⑤⑥⑦⑧⑨⑩])\\s*(.+)$/);
            const bold = line.match(/^\\*\\*(.+?)\\*\\*$/);
            if(num){ cards.push({t: num[2].trim()}); }
            else if(circle){ cards.push({t: circle[2].trim()}); }
            else if(bold){ cards.push({t: bold[1].trim()}); }
            else if(line.length){ cards.push({t: line}); }
        });
        const isAdvice = /建议|优化|修改|改法|方向|行动|提升|加强|关注/.test(sec.title);
        if(isAdvice){
            html += `<div class="viz-cards">`;
            cards.forEach((c,i)=>{
                html += `<div class="viz-card"><span class="viz-no">${i+1}</span><span class="viz-txt">${aiInline(c.t)}</span></div>`;
            });
            html += `</div>`;
        }
    });
    return html;
}"""

new = """// 把 AI 文本解析为可视化元素：建议章节→卡片网格；解析不到建议章节时保底全部显示，避免空白
function renderAiVisual(text){
    const raw = String(text==null?"":text).trim();
    if(!raw) return "<div class='muted'>（无返回）</div>";
    if(/失败|error/i.test(raw) && raw.length < 160){
        return `<div class="muted" style="color:#c0392b">${esc(raw)}</div>`;
    }
    const lines = raw.split(/\\r?\\n/).map(l=>l.trim()).filter(l=>l.length>0);
    if(!lines.length) return "<div class='muted'>（无返回）</div>";
    const secs = [];
    let cur = null;
    lines.forEach(line=>{
        // 兼容多种章节写法：**一、xxx** / ## xxx / 1. xxx / 一、xxx
        let rl = line.replace(/^\\*{1,2}/, "").replace(/\\*{1,2}$/, "").trim();
        const m = rl.match(/^(第?[一二三四五六七八九十百]+|[1-9][0-9]?)[、.．:：)）]\\s*(.+)$/)
               || rl.match(/^#{1,6}\\s*(.+)$/);
        if(m){
            let title = (m[2] || m[1] || "").trim();
            const cm = title.match(/^([^：:]{1,10})[：:]\\s*(.+)$/);
            if(cm){ title = cm[1].trim(); }
            else{ title = title.replace(/[：:]\\s*$/, ""); }
            cur = {title: title, items: []};
            secs.push(cur);
            if(cm && cm[2].trim()){ cur.items.push(cm[2].trim()); }
        }else{
            if(!cur){ cur = {title: "AI 结论", items: []}; secs.push(cur); }
            cur.items.push(line);
        }
    });
    if(!secs.length){ secs.push({title: "AI 结论", items: lines}); }
    const ADV = /建议|优化|修改|改法|方向|行动|提升|加强|关注/;
    // 保底：解析不到任何建议章节时，全部章节都渲染为卡片，保证"点评与修改建议"区域有内容
    const hasAdvice = secs.some(sec=>ADV.test(sec.title));
    let html = "";
    secs.forEach(sec=>{
        if(hasAdvice && !ADV.test(sec.title)){ return; }
        html += `<div class="viz-sec"><span class="viz-sec-tag">${esc(sec.title)}</span></div>`;
        const cards = [];
        sec.items.forEach(line=>{
            const num = line.match(/^(\\d+)[、.．:：)）]\\s*(.+)$/);
            const circle = line.match(/^([①②③④⑤⑥⑦⑧⑨⑩])\\s*(.+)$/);
            const bold = line.match(/^\\*\\*(.+?)\\*\\*$/);
            if(num){ cards.push({t: num[2].trim()}); }
            else if(circle){ cards.push({t: circle[2].trim()}); }
            else if(bold){ cards.push({t: bold[1].trim()}); }
            else if(line.length){ cards.push({t: line}); }
        });
        html += `<div class="viz-cards">`;
        cards.forEach((c,i)=>{
            html += `<div class="viz-card"><span class="viz-no">${i+1}</span><span class="viz-txt">${aiInline(c.t)}</span></div>`;
        });
        html += `</div>`;
    });
    return html;
}"""

assert old in t, "renderAiVisual 未匹配"
t = t.replace(old, new, 1)

io.open(p, "w", encoding="utf-8").write(t)
print("renderAiVisual 增强 ✓")
