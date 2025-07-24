#!/usr/bin/env python3

import json
import logging
import re
import os
import requests
import urllib.parse
from typing import Union, List, Annotated
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

origins = [
    "https://mscat-medieval-production.medieval.lib.cam.ac.uk",
    "https://medieval.lib.cam.ac.uk"
]

logger = logging.getLogger('gunicorn.error')

if 'SOLR_HOST' in os.environ:
    SOLR_HOST = os.environ['SOLR_HOST']
else:
    print('ERROR: SOLR_HOST environment variable not set')

if 'SOLR_PORT' in os.environ:
    SOLR_PORT = os.environ['SOLR_PORT']
else:
    print('WARN: SOLR_PORT environment variable not set')

SOLR_URL = 'http://%s:%s' % (SOLR_HOST, SOLR_PORT)

INTERNAL_ERROR_STATUS_CODE = 500

# Core names
ITEM_CORE = 'mscat'
COLLECTION_JSON_CORE = 'collection'

ALLOWED_FACETS = ['author_sm', 'editor_sm', 'lang_sm', 'ms_date_sm', 'ms_datecert_s', 'ms_origin_sm', 'wk_subjects_sm', 'ms_materials_sm', 'ms_decotype_sm', 'ms_music_b', 'ms_bindingdate_sm', 'ms_digitized_s', 'ms_repository_s', 'ms_collection_s']

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_core_name(resource_type: str):
    core = ''

    resource_type_trimmed = re.sub(r's$', '', resource_type)
    if resource_type_trimmed == 'item':
        core = ITEM_CORE
    elif resource_type_trimmed == 'collection':
        core = COLLECTION_JSON_CORE

    return core


def get_fieldprefix(val):
    result: str = '_text_'
    if val == 'text':
        result = 'content.html'
    return result

def get_obj_property(key, param):
    result = None
    if key in param:
        result = param[key]
    return result


def stringify(p):
    result = None
    if type(p) == list:
        result = " ".join(p)
    elif not type(p) in [dict, tuple]:
        result = str(p)
    return result

def listify(p):
    result = []
    if type(p) is str:
        result.append(p)
    elif type(p) is list:
        result = p
    else:
        result.append(p)
    return result


def translate_params(resource_type: str, **url_params):
    translation_key = {
        'transcribed': 'content_textual-content',
        'footnote': 'content_footnotes',
        'summary': 'content_summary',
    }
    solr_delete = ['text','keyword', 'sectionType', 'search-date-type']
    solr_fields = ['_text_', 'content_textual-content', 'content_footnotes', 'content_summary']

    # day and month are removed from the set_params array during date processing -- unless they are absolutely
    # necessary for weird impartial searches -- like searching for every letter written in a november.
    # This sort or search doesn't even work on the current site.
    remap_fields = ['day', 'month', 'dateRange']
    solr_params = { }
    q = []
    fq = []
    filter={}
    solr_name = ''

    set_params = {k: v for k, v in url_params.items() if v}

    # Tidy compound variable searches
    date_min=generate_datestring(get_obj_property('year', set_params),
                                 get_obj_property('month', set_params),
                                 get_obj_property('day', set_params))
    date_max=generate_datestring(get_obj_property('year-max', set_params),
                                 get_obj_property('month-max', set_params),
                                 get_obj_property('day-max', set_params))

    search_date_type = get_obj_property('search-date-type', set_params)

    #print('%s - %s ' % (date_min, date_max))
    if date_min or search_date_type == 'between':
        for x in ['year', 'month', 'day', 'year-max', 'month-max', 'day-max', 'search-date-type']:
            set_params.pop(x, None)

        result = None
        predicate_type = 'Within'
        if date_max or search_date_type == 'between':
            #print('Max date provided or implied')
            date_max_final = date_max if date_max else '2009-02-12'
            date_min_final = date_min if date_min else '1609-02-12'
            predicate_type = 'Intersects'
            result ='[%s TO %s]' % (date_min_final, date_max_final)
        else:
            #print('Min date only with %s' % search_date_type)
            result = date_min
            if search_date_type in 'after':
                predicate_type = 'Intersects'
                result = '[%s TO 2009-02-12]' % date_min
            elif search_date_type == 'before':
                predicate_type = 'Intersects'
                result = '[-3000-01-01 TO %s]' % date_min
            else:
                result = date_min

        if result:
            fq.append('{!field f=dateRange op=%s}%s' % (predicate_type, result))
    else:
        set_params.pop('search-date-type', None)
    #NB: Advanced search set f1-document-type=letter

    for name in set_params.keys():
        if get_obj_property(name, set_params):
            #print('Processing %s' % name)
            value = set_params[name]

            if name in remap_fields:
                #print('adding %s="%s" to q' % (name,value))
                val_string: str = stringify(value)
                value_final = "(%s)" % val_string
                q.append(":".join([name,value_final]))
                #print(q)
            elif name in ['keyword','text']:
                #print('adding ' + name + ' to q')
                val_string: str = stringify(value)
                value_final = "(%s)" % val_string
                q.append(value_final)
            elif re.match(r'^f[0-9]+-date$', name):
                val_list = value
                val_list.sort()
                fields = ['facet-year', 'facet-year-month', 'facet-year-month-day']
                for date in val_list:
                    date = re.sub(r'^"(.+?)"$', r'\1', date)
                    date_parts = date.split('::')
                    num_parts = len(date_parts)
                    if num_parts == 1:
                        solr_name = 'facet-year'
                    elif num_parts == 2:
                        solr_name = 'facet-year-month'
                    elif num_parts == 3:
                        solr_name = 'facet-year-month-day'
                    contains_nested = fields[num_parts:]
                    contains_parent_or_self = fields[:(num_parts)-1]
                    for index, field in enumerate(fields):
                        if (num_parts-1) <= index:
                            filter['f.%s.facet.contains' % field ] = re.sub(r'^"(.+?)"$', r'\1', date)
                        else:
                            d = "::".join(date_parts[:(index+1)])
                            filter['f.%s.facet.contains' % field ] = re.sub(r'^"(.+?)"$', r'\1', d)
                    fq.append('%s:"%s"' % (solr_name, re.sub(r'^"(.+?)"$', r'\1', date)))
            elif name in ALLOWED_FACETS:
                # match old-style xtf facet names f\d+-
                solr_name = re.sub(r'^f[0-9]+-(.+?)$',r'facet-\1', name)
                for x in listify(value):
                    fq.append('%s:"%s"' % (solr_name, re.sub(r'^"(.+?)"$', r'\1', x)))
            elif re.match(r'^(facet|s)-.+?$', name):
                # Add facet params starting facet- or s- (only allowed on site)
                fq.append('%s:"%s"' % (name, re.sub(r'^"(.+?)"$', r'\1', value)))
            elif name == 'page':
                page = int(set_params['page'])
                start = (page - 1) * 20
                solr_params['start'] = start
            elif name == 'sort':
                sort_raw = set_params['sort'][0]
                sort_val: str = ''
                if sort_raw in ['title', 'date']:
                    sort_val = sort_raw
                else:
                    sort_val = 'score'
                sort_order = 'desc' if sort_val == 'score' else 'asc'
                solr_params['sort'] = ' '.join([sort_val, sort_order])
            elif name != "rows":
                val_string: str = stringify(value)
                value_final = "(%s)" % val_string
                q.append(":".join([name,value_final]))

    solr_params['fq'] = fq

    # Hack to ensure that empty q string or * returns all records
    # The whole code that generates the query string will need to be re-examined to deal with this better
    final_q = ' '.join(q)
    if final_q in ["['*']", "['']"]:
        final_q = '*'
    solr_params['q'] = final_q
    if solr_params['q'] in ["['*']", "['']"]:
        solr_params['q'] = '*'
    solr_params = solr_params | filter
    for i in solr_delete + solr_fields:
        solr_params.pop(i, None)
    #print('SET PARAMS:')
    #print(set_params)
    #print('FINAL PARAMS:')
    #print(solr_params)
    return solr_params


def generate_datestring(year, month, day):
    result=None
    if year:
        #print(year, month, day)
        # If it's possible to create a valid dateRange token, do so and delete individual date params
        if not (day and not month):
            valid_tokens = [str(i).zfill(2) for i in [year, month, day] if i is not None]
            start_date = '-'.join(valid_tokens)
            result = start_date
        return result


async def delete_resource(resource_type: str, file_id: str):
    delete_query = 'id:"%s"' % urllib.parse.unquote_plus(file_id)
    delete_cmd = {'delete': {'query': delete_query}}
    core = get_core_name(resource_type)
    if core:
        r = requests.post(url="%s/solr/%s/update" % (SOLR_URL, core),
                          headers={"content-type": "application/json; charset=UTF-8"},
                          json=delete_cmd,
                          timeout=60)
        status_code = r.status_code
    else:
        status_code = INTERNAL_ERROR_STATUS_CODE

    return status_code


async def get_request(resource_type: str, **kwargs):
    core = get_core_name(resource_type)
    try:
        params = kwargs.copy()
        solr_params = translate_params(core, **params)
        start = (int(solr_params['page']) - 1) * 20 if 'page' in solr_params else 0
        try:
            del solr_params.page
        except AttributeError:
            pass
        #print(solr_params)
        r = requests.get("%s/solr/%s/spell" % (SOLR_URL, core), params=solr_params, timeout=60)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        if hasattr(e.response, 'text'):
            results = json.loads(e.response.text)
            raise HTTPException(status_code=results["responseHeader"]["status"], detail=results["error"]["msg"])
        else:
            raise HTTPException(status_code=502, detail=str(e).split(':')[-1])
    result = r.json()
    return result


async def put_item(resource_type: str, data, params):
    core = get_core_name(resource_type)
    path = 'update/json/docs'
    try:
        r = requests.post(url="%s/solr/%s/%s" % (SOLR_URL, core, path),
                          params=params,
                          headers={"content-type": "application/json; charset=UTF-8"},
                          data=data,
                          timeout=60)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise e
    status_code = r.status_code

    return status_code


# Does FastAPI escape params automatically?
def ensure_urlencoded(var, safe=''):
    if type(var) is str:
        return urllib.parse.quote(urllib.parse.unquote(var, safe))
    elif type(var) is dict:
        dict_new = {}
        for key, value in var.items():
            if value is not None:
                value_final = ''
                if type(value) is str:
                    value_final = urllib.parse.quote(urllib.parse.unquote(value), safe=safe)
                elif type(value) is list:
                    values = []
                    for i in value:
                        values.append(urllib.parse.quote(urllib.parse.unquote(i), safe=safe))
                    value_final = values
                dict_new.update({key: value_final})
        return dict_new


@app.get("/collections")
async def get_collections(q: List[str] = Query(default=None),
                          fq: List[str] = Query(default=None),
                          sort: Union[str, None] = None,
                          page: Union[str, None] = None,
                          rows: Union[int, None] = None):
    q_final = ' AND '.join(q) if hasattr(q, '__iter__') else q
    rows_final = rows if rows in [8, 20] else 20

    # Limit params passed through to SOLR
    # Add facet to exclude collections from results
    params = {"q": q_final, "fq": fq, "sort": sort, "page": page, "rows": rows_final}
    r = await get_request('collections', **params)
    return r


@app.get("/items")
async def get_items(request: Request,
                    sort: Union[str, None] = None,
                    page: Union[int, None] = 1,
                    rows: Union[int, None] = None,
                    keyword: List[str] = Query(default=[]),
                    ms_title_t: List[str] = Query(default=[]),
                    name_t: List[str] = Query(default=[]),
                    author_sm: Annotated[list[str] | None, Query()] = None,
                    editor_sm: Annotated[list[str] | None, Query()] = None,
                    lang_sm: Annotated[list[str] | None, Query()] = None,
                    ms_date_sm: Annotated[list[str] | None, Query()] = None,
                    ms_datecert_s: Annotated[list[str] | None, Query()] = None,
                    ms_origin_sm: Annotated[list[str] | None, Query()] = None,
                    wk_subjects_sm: Annotated[list[str] | None, Query()] = None,
                    ms_materials_sm: Annotated[list[str] | None, Query()] = None,
                    ms_decotype_sm: Annotated[list[str] | None, Query()] = None,
                    ms_music_b: Annotated[list[str] | None, Query()] = None,
                    ms_bindingdate_sm: Annotated[list[str] | None, Query()] = None,
                    ms_digitized_s: Annotated[list[str] | None, Query()] = None,
                    ms_repository_s: Annotated[list[str] | None, Query()] = None,
                    ms_collection_s: Annotated[list[str] | None, Query()] = None,
                    facet_searchable: Annotated[list[str] | None, Query()] = None):

    facets = {}
    for x in filter(lambda x: x in ALLOWED_FACETS, request.query_params.keys()):
        facets[x]=request.query_params.getlist(x)
    rows = rows if rows in [8, 20] else 20
    # Limit params passed through to SOLR
    # Add facet to exclude collections from results
    params = {"sort": sort,
        #"start": start,
        "page": page,
        "rows": rows,
        "keyword": request.query_params.getlist('keyword'),
        "ms_title_t": request.query_params.getlist('ms_title_t'),
        "name_t": request.query_params.getlist('name_t')
    }
    r = await get_request('items', **params, **facets)
    return r


@app.get("/summary")
async def get_summary(q: List[str] = Query(default=None),
                fq: Union[str, None] = None):
    q_final = ' AND '.join(q) if hasattr(q, '__iter__') else q

    # Very few params are relevant to the summary view
    params = {"q": q_final, "fq": fq}

    r = await get_request('items', **params)

    # This query returns the first page of results and the areas of the response that will
    # principally be useful are the responseHeader, response (but not response > docs),
    # facet_counts and possibly highlighting (i.e. snippets).
    # Ultimately, we would change the data structure at this point to the common
    # format needed. Rather than spend time doing  this, I just deleted the docs, which
    # we wouldn't use in this view
    del r['response']['docs']
    return r


# All destructive requests (post, put, delete) will be in a separate API
# that's kept in a private subnet. All access to them would be limited to
# the services that require them (CUDL Indexer - for post, SNS Message on
# deletion of a TEI file in cudl-source-data).
@app.put("/collection")
async def update_collection(request: Request):
    # Receive data via a data-binary curl request from the CUDL Indexer lambda
    data = await request.body()

    # status_code = ''
    json_dict = json.loads(data)
    if json_dict['name']:
        url_slug = json_dict['name']["url-slug"]
        logger.info(f"Indexing %s" % url_slug)
        status_code = await put_item('collection', data, {'f': ['$FQN:/**', 'id:/name/url-slug']})
    else:
        logger.info(f"ERROR: Collection JSON does not seem to conform to expectations")
        # I wasn't sure what status_code to use for invalid document.
        status_code = INTERNAL_ERROR_STATUS_CODE
    return status_code


@app.put("/item")
async def update_item(request: Request):
    # Receive data via a data-binary curl request from the CUDL Indexer lambda
    data = await request.body()

    json_dict = json.loads(data)

    logger.info(f"Indexing %s" % json_dict['id'])
    status_code = await put_item('item', data, {})
    return status_code


@app.delete("/item/{file_id}")
async def delete_item(file_id: str):
    return await delete_resource('item', file_id)


@app.delete("/collection/{file_id}")
async def delete_collection(file_id: str):
    return await delete_resource('collection', file_id)
