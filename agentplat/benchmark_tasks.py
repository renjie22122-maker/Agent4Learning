"""Additional held-out task families; grader programs are never put in author workspace."""
TASKS = {
 'json_merge':dict(category='data',artifact='merge.py',files={},
  prompt='实现 merge.py 的 merge(base, patch)：两个字典递归合并；patch 中 None 删除对应键；字典递归，其他值（含列表）整体替换。不能修改两个输入，返回结果也不能共享输入的可变子对象。用标准库测试。',
  check="""from merge import merge
import copy
a={'x':{'a':1,'b':[1]},'remove':2};b={'x':{'a':None,'z':3},'remove':None}
aa=copy.deepcopy(a);bb=copy.deepcopy(b)
c=merge(a,b);assert c=={'x':{'b':[1],'z':3}};assert a==aa and b==bb
c['x']['b'].append(2);assert a==aa
assert merge({}, {'x':{'a':None,'b':2}})=={'x':{'b':2}}
assert merge({'x':[1]}, {'x':[]})=={'x':[]}
"""),
 'topological_order':dict(category='coding',artifact='graph.py',files={},
  prompt='实现 graph.py 的 order(edges)：edges 为字符串节点到依赖节点列表的 dict，依赖必须先输出；仅在依赖列表中的节点也需包含；多个可用节点按字典序选最小者；有环抛 ValueError；空图返回[]，不能修改输入。',
  check="""from graph import order
import copy
g={'c':['a','b'],'b':['a'],'a':[]};before=copy.deepcopy(g)
assert order(g)==['a','b','c'];assert g==before
assert order({'z':['x'],'a':[]})==['a','x','z'];assert order({})==[]
for g in ({'a':['a']},{'a':['b'],'b':['a']}):
 try:order(g)
 except ValueError:pass
 else:raise AssertionError('cycle accepted')
"""),
 'safe_paths':dict(category='security',artifact='paths.py',files={},
  prompt='实现 paths.py 的 safe_relative(path)：只接受使用 / 分隔的安全相对路径并原样返回。拒绝空串、绝对路径、反斜杠、冒号、NUL、空段、. 和 .. 段（抛 ValueError）。允许中文和文件名里的点，不操作实际文件系统。',
  check="""from paths import safe_relative
for p in ['a/b.txt','中文/a.b','a..b']:assert safe_relative(p)==p
for p in ['', '/', '/a', 'a//b','a/','./x','a/../b','..','C:x','x:y','a\\\\b','a\\x00b']:
 try:safe_relative(p)
 except ValueError:pass
 else:raise AssertionError(repr(p))
"""),
 'unicode_counts':dict(category='data',artifact='words.py',files={},
  prompt='实现 words.py 的 counts(text)：先做 Unicode NFC 规范化，再按任意空白切词，对每个词 casefold 后计数，返回字典。保留标点；空文本返回{}。',
  check="""from words import counts
assert counts('Straße STRASSE')=={'strasse':2}
assert counts('café cafe\\u0301')=={'café':2}
assert counts('甲\\t甲\\n乙')=={'甲':2,'乙':1}
assert counts('A,a A')=={'a,a':1,'a':1};assert counts('  ')=={}
"""),
 'date_ranges':dict(category='coding',artifact='dates.py',files={},
  prompt='实现 dates.py 的 days(start,end)：两个 YYYY-MM-DD 日期，返回包含首尾的 ISO 日期字符串列表；start>end 返回[]；非法日期抛 ValueError。用标准库，覆盖闰年。',
  check="""from dates import days
assert days('2024-02-28','2024-03-01')==['2024-02-28','2024-02-29','2024-03-01']
assert days('2023-12-31','2024-01-01')==['2023-12-31','2024-01-01']
assert days('2024-01-02','2024-01-01')==[]
try:days('2023-02-29','2023-03-01')
except ValueError:pass
else:raise AssertionError('invalid date accepted')
"""),
 'followup_pagination':dict(category='multi_turn',artifact='pages.py',files={},
  prompt='实现 pages.py 的 page(items,number,size)：页码从1开始，返回对应切片列表。number或size小于1抛ValueError，超过范围返回[]，不能修改items。测试后完成。',
  followup='增加函数 pages(items,size)，返回所有分页列表，空输入返回[]；size<1抛ValueError。保留原来的page函数及行为。',
  check="""from pages import page,pages
assert page([1,2,3],2,2)==[3];assert page([1],3,2)==[]
assert pages([1,2,3,4,5],2)==[[1,2],[3,4],[5]];assert pages([],2)==[]
x=[1,2,3];page(x,1,2);pages(x,2);assert x==[1,2,3]
for f,args in [(page,([],0,2)),(page,([],1,0)),(pages,([],0))]:
 try:f(*args)
 except ValueError:pass
 else:raise AssertionError('bad bound accepted')
"""),
 'rag_conflict':dict(category='rag',artifact='result.json',files={},check=None,
  prompt='从任务知识库查询上海地区2026年9月生效的住宿限额。写result.json，键hotel_limit（整数）、currency、source（实际kb:分块引用）。地区特别政策优先通用政策，过期政策无效。',
  knowledge={'current.txt':'地区特别政策：上海，2026年9月生效，住宿限额每日920元，币种CNY；优先于通用政策。',
             'general.txt':'通用政策：2026年9月，住宿每日680元，CNY。',
             'old.txt':'上海旧版2024年政策已废止：住宿500元。'},expected_limit=920),
 'rag_large':dict(category='rag',artifact='result.json',files={},check=None,
  prompt='在任务知识库里查找北区项目青鹭的最新住宿政策（2026年9月），写result.json，键hotel_limit、currency、source（实际kb:分块引用）。其他项目和已废止版本不能作为答案。',
  knowledge={**{f'noise{i}.txt':f'北区项目青鹭归档政策{i}（已废止），住宿每日{100+i}元，CNY。' for i in range(80)},
             'current.txt':'北区项目青鹭现行政策，2026年9月生效，替代全部归档版本。住宿每日735元，币种CNY。'},expected_limit=735),
}
