"""受控展示模板：模型只能给结构化表格数据，不能下发网页或原生脚本。"""
import html
from fastapi import HTTPException

TOOL = {'type':'function','function':{'name':'render_table','description':'将已获得的结果展示为受控的可横向滚动表格，不能执行任务或加载外部网页。','parameters':{'type':'object','properties':{'title':{'type':'string'},'columns':{'type':'array','items':{'type':'string'}},'rows':{'type':'array','items':{'type':'array','items':{'type':'string'}}}},'required':['title','columns','rows'],'additionalProperties':False}}}


def render(arguments):
    if set(arguments) != {'title','columns','rows'}:
        raise HTTPException(422,'展示参数不符合模板')
    title, columns, rows = arguments['title'], arguments['columns'], arguments['rows']
    if not isinstance(title,str) or len(title)>100 or not isinstance(columns,list) or not 1<=len(columns)<=8 or not isinstance(rows,list) or len(rows)>50:
        raise HTTPException(422,'表格大小超过限制')
    if any(not isinstance(column,str) or len(column)>100 for column in columns) or any(not isinstance(row,list) or len(row)!=len(columns) or any(not isinstance(cell,str) or len(cell)>1000 for cell in row) for row in rows):
        raise HTTPException(422,'表格单元格格式无效')
    csp="default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; frame-src 'none'; base-uri 'none'"
    header=''.join('<th>'+html.escape(column)+'</th>' for column in columns)
    content=''.join('<tr>'+''.join('<td>'+html.escape(cell)+'</td>' for cell in row)+'</tr>' for row in rows)
    page='<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="'+html.escape(csp,quote=True)+'"><style>:root{color-scheme:light dark}body{font:16px -apple-system,sans-serif;margin:12px}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid #8886;white-space:pre-wrap}h3{font-size:18px}</style><h3>'+html.escape(title)+'</h3><table><thead><tr>'+header+'</tr></thead><tbody>'+content+'</tbody></table></html>'
    if len(page.encode())>65536:
        raise HTTPException(413,'展示内容超过64KiB')
    return {'status':'rendered','parts':[{'type':'h5','title':title,'html':page,'template':'table.v1'}], 'text':title}
