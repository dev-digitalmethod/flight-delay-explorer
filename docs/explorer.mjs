import {evaluate} from "./analytics.mjs";

const $ = id => document.getElementById(id);
const fmt = new Intl.NumberFormat("en-US", {maximumFractionDigits: 2});
const theme = getComputedStyle(document.documentElement);
const color = name => theme.getPropertyValue(name).trim();
const colors = [color("--teal"), "#496a9c", "#ad541e", "#7a4d97", "#30753a", "#a33655"];
const names = {mean:"Mean", median:"Median", p95:"P95", count:"Valid count", month:"monthly"};
const stats = ["n","missing","outside","mean","median","p95","stddev","min","max"];
const filters = ["airline", "origin"];
let dataset;
let chosen = [];
let current;
let visible = new Set();
let pending = false;
let revision = 0;

function groups() {
  const mode = $("group").value;
  $("group-options").hidden = !mode;
  $("group-add").replaceChildren(new Option("Add a group…", ""));
  for (const code of dataset.values[mode] || []) {
    if (!chosen.includes(code)) $("group-add").add(new Option(code, code));
  }
  $("group-add").disabled = chosen.length >= 5;
  $("chosen").replaceChildren(...chosen.map(code => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = code + " ×";
    chip.setAttribute("aria-label", "Remove " + code);
    chip.onclick = () => { chosen = chosen.filter(item => item !== code); groups(); };
    return chip;
  }));
  $("group-hint").textContent = chosen.length ? "Choose up to five groups." : mode === "origin"
    ? "Defaults to the filtered airport or the three busiest airports in the dataset."
    : "Defaults to the three airlines with the most valid values.";
}

function note(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
}

function setBusy(busy) {
  pending = busy;
  $("settings").disabled = busy;
  $("run").disabled = busy;
  $("run").textContent = busy ? "Evaluating…" : "Evaluate";
}

function legend() {
  const container = $("series");
  container.hidden = !current.series.length;
  container.replaceChildren(...current.series.map((series, index) => {
    const label = document.createElement("label");
    label.className = "check";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = visible.has(series.id);
    box.onchange = () => {
      if (box.checked) visible.add(series.id);
      else visible.delete(series.id);
      render().catch(error => note(error.message, true));
    };
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.backgroundColor = colors[index];
    label.append(box, swatch, series.label);
    return label;
  }));
}

async function render() {
  const c = current;
  const series = c.series.map((item, index) => ({...item, color: colors[index]}))
    .filter(item => visible.has(item.id));
  const tbody = $("summary");
  tbody.replaceChildren(...series.map(item => {
    const tr = document.createElement("tr");
    const name = document.createElement("th");
    name.scope = "row";
    name.textContent = item.label;
    tr.append(name);
    for (const stat of stats) {
      const td = document.createElement("td");
      td.textContent = item.summary[stat] === null ? "—" : fmt.format(item.summary[stat]);
      tr.append(td);
    }
    return tr;
  }));
  const central = $("distribution-range").value === "central";
  const edges = central ? c.centralEdges : c.edges;
  const centers = edges.slice(1).map((edge, index) => (edge + edges[index]) / 2);
  const widths = edges.slice(1).map((edge,index) => edge - edges[index]);
  const shares = $("scale").value === "share";
  const signal = c.label + " (" + c.unit + ")";
  const base = {
    autosize:true, showlegend:false, font:{family:color("--font"),color:color("--muted"),size:12},
    margin:{l:62,r:18,t:30,b:52}, paper_bgcolor:color("--surface"), plot_bgcolor:color("--surface"), uirevision:revision,
  };
  const axis = {automargin:true,gridcolor:color("--line"),zerolinecolor:"#a8bbb6"};
  const distributionAxis = edges.length ? {range:[edges[0],edges.at(-1)]} : {autorange:true};
  const config = {responsive:true,displaylogo:false,modeBarButtonsToRemove:["lasso2d","select2d"]};
  const trend = series.map(s => ({
    type:"scatter",mode:c.dates.length <= 126 ? "lines+markers" : "lines",name:s.label,
    line:{color:s.color,width:2},marker:{size:4},connectgaps:false,x:c.dates,y:s.time,customdata:s.timeCounts,
    hovertemplate:"%{x|%b %Y}<br>%{y:,.2f}<br>%{customdata:,} valid<extra>%{fullData.name}</extra>"
  }));
  const hist = series.filter(s => s.summary.n).map(s => {
    const counts = central ? s.centralHistogram : s.histogram;
    return {
      type:"bar",name:s.label,x:centers,width:widths,y:shares ? counts.map(n=>n/s.summary.n) : counts,
      marker:{color:s.color,line:{color:s.color,width:1}},opacity:series.length === 1 ? .8 : .44,
      customdata:counts.map((n,i)=>[edges[i],edges[i+1],n]),
      hovertemplate:"%{customdata[0]:.2f} to %{customdata[1]:.2f}<br>%{customdata[2]:,} values"
        + (shares ? " / %{y:.2%}" : "") + "<extra>%{fullData.name}</extra>"
    };
  });
  const ecdf = series.filter(s => s.summary.n).map(s => ({
    type:"scatter",mode:"lines",name:s.label,x:s.ecdfX,y:s.ecdfY,line:{color:s.color,width:2,shape:"hv"},
    hovertemplate:"%{x:,.2f}<br>%{y:.2%} at or below<extra>%{fullData.name}</extra>"
  }));
  await Promise.all([
    Plotly.react("trend",trend,{...base,hovermode:"x unified",xaxis:{...axis,type:"date",title:{text:"Flight month"}},
      yaxis:{...axis,title:{text:c.aggregate==="count" ? "Valid observations" : names[c.aggregate] + " (" + c.unit + ")"}}},config),
    Plotly.react("histogram",hist,{...base,uirevision:revision+"-"+central+"-"+shares,barmode:"overlay",xaxis:{...axis,...distributionAxis,title:{text:signal}},
      yaxis:{...axis,tickformat:shares ? ".0%" : ",d",title:{text:shares ? "Share within series" : "Count"}}},config),
    Plotly.react("ecdf",ecdf,{...base,uirevision:revision+"-"+central,xaxis:{...axis,...distributionAxis,title:{text:signal}},
      yaxis:{...axis,range:[0,1.02],tickformat:".0%",title:{text:"Cumulative share"}}},config),
  ]);
}

function limit(id) {
  const input = $(id);
  return input.value === "" ? null : input.valueAsNumber;
}

async function run() {
  if (pending || !dataset || !dataset.months.length || !$("controls").reportValidity()) return;
  const body = {
    start:$("start").value,end:$("end").value,
    aggregate:$("aggregate").value,group:$("group").value,groups:chosen,total:$("total").checked,
    outcome:$("outcome").value,filters:Object.fromEntries(filters.map(id=>[id,$(id).value])),
    min:limit("minimum"),max:limit("maximum"),
  };
  setBusy(true);
  $("charts").hidden = true;
  $("series").hidden = true;
  $("coverage").hidden = true;
  note("Evaluating selected flights…");
  try {
    current = await evaluate(body, dataset);
    revision++;
    visible = new Set(current.series.map(item=>item.id));
    const coverage = $("coverage");
    coverage.hidden = !current.missingMonths.length;
    coverage.textContent = "Missing months in this period · " + current.missingMonths.join(", ");
    const hasData = current.series.some(item=>item.summary.n);
    note(hasData
      ? current.label + " · " + body.start + " to " + body.end + " · " + names[current.grain] + " " + names[current.aggregate].toLowerCase()
      : "No valid signal values match the period and filters.");
    legend();
    $("charts").hidden = !current.series.length;
    if (current.series.length) await render();
  } catch (error) {
    $("charts").hidden = true;
    $("series").hidden = true;
    note(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function init() {
  try {
    if (!window.Plotly) throw new Error("The chart library could not load. Reload to try again.");
    const response = await fetch("./data/manifest.json", {cache: "no-cache"});
    if (!response.ok) throw new Error("The flight dataset could not load. Reload to try again.");
    dataset = await response.json();
    const first = dataset.months[0];
    const last = dataset.months.at(-1);
    const monthName = new Intl.DateTimeFormat("en-US", {month: "long", year: "numeric", timeZone: "UTC"});
    const displayMonth = month => monthName.format(new Date(month + "-01T00:00:00Z"));
    $("source").textContent = displayMonth(first) + " to " + displayMonth(last) + " · " + dataset.months.length + " months of BTS data";
    for (const id of ["start", "end"]) {
      $(id).min = first;
      $(id).max = last;
    }
    const defaultStart = "2024-01";
    $("start").value = first <= defaultStart && defaultStart <= last ? defaultStart : first;
    $("end").value = last;
    for (const id of filters) {
      $(id).replaceChildren(new Option("All", ""), ...dataset.values[id].map(code => new Option(code, code)));
    }
    if (dataset.values.origin.includes("RDU")) $("origin").value = "RDU";
    setBusy(false);
    await run();
  } catch (error) {
    $("source").textContent = "Could not load data";
    note(error.message,true);
  }
}
$("controls").onsubmit = event => { event.preventDefault(); run(); };
$("group").onchange = () => { chosen = []; groups(); };
$("group-add").onchange = () => {
  if ($("group-add").value && chosen.length < 5) chosen.push($("group-add").value);
  groups();
};
$("scale").onchange = () => { if(current) render().catch(error => note(error.message,true)); };
$("distribution-range").onchange = $("scale").onchange;
window.addEventListener("DOMContentLoaded",init);
