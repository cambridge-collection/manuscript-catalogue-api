#!/usr/bin/env python3

from typing import Annotated, Union, List
from fastapi import Body, FastAPI, Request, Query
import requests, json, urllib.parse, logging

logger = logging.getLogger(__name__)

SOLR_HOST = "localhost"
SOLR_PORT = 8983

GET = "http://%s:%s/solr/cdcp/select" % (SOLR_HOST, SOLR_PORT)
PUT = "http://%s:%s/solr/cdcp/update/json/docs?split=/pages&f=/pages/* " % (SOLR_HOST, SOLR_PORT)

app = FastAPI()


def ensure_urlencoded(var, safe=''):
    if type(var) == str:
        return urllib.parse.quote(urllib.parse.unquote(var, safe))
    elif type(var) == dict:
        dict_new = {}
        for key, value in var.items():
            if value is not None:
                print(type(value), value)
                if type(value) is str:
                    value_final = urllib.parse.quote(urllib.parse.unquote(value), safe=safe)
                elif type(value) is list:
                    values = []
                    for i in value:
                        values.append(urllib.parse.quote(urllib.parse.unquote(i), safe=safe))
                    value_final = values
                dict_new.update({key: value_final})
        return dict_new


@app.get("/items")
def get_request(q: Union[str, None] = '*',
                fq: List[str] = Query(default=None),
                sort: Union[str, None] = None,
                start: Union[str, None] = None):

    # Apply default query so facet-based wayfinding is possible
    q_final = '*' if not q.strip() else q

    # Limit params passed through to SOLR
    params={"q": q_final, "fq": fq, "sort": sort, "start": start}
    r = requests.get(GET, params=params)

    return r.json()


@app.get("/summary")
def summarise_request(q: Union[str, None] = '*',
                      fq: Union[str, None] = None):
    # Apply default query so facet-based wayfinding is possible
    q_final = '*' if not q.strip() else q

    # Very few params are relevant to the summary view
    params = {"q": q_final, "fq": fq}

    r = requests.get(GET, params=params)
    results_json = r.json()

    # This query returns the first page of results and the areas of the response that will
    # principally be useful are the responseHeader, response (but not response > docs),
    # facet_counts and possibly highlighting (ie. snippets).
    # Ultimately, we would change the data structure at this point to the common
    # format needed. Rather than spend time doing  this, I just deleted the docs, which
    # we wouldn't use in this view
    del results_json['response']['docs']

    return results_json


# All destructive requests (post, put, delete) will be in a separate API
# that's kept in a private subnet. All access to them would be limited to
# the services that require them (CUDL Indexer - for post, SNS Message on
# deletion of a TEI file in cudl-source-data).
@app.post("/post")
async def post_request(request: Request):
    # Receive data via a data-binary curl request from the CUDL Indexer lambda
    data = await request.body()

    status_code = ''
    json_dict = json.loads(data)
    if json_dict['descriptiveMetadata']:
        logger.info(f"Indexing %s" % json_dict['fileID'])
        try:
            r = requests.post(url="http://%s:%s/solr/cdcp/update/json/docs" % (SOLR_HOST, SOLR_PORT),
                              params={'split': '/pages', 'f': ['/pages/*', '/*']},
                              headers={"content-type": "application/json; charset=UTF-8"},
                              data=data)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise e
        status_code = r.status_code
    else:
        logger.info(f"JSON does not seem to conform to expectations: %s" % json_dict['fileID'])
        # I wasn't sure what status_code to use for invalid document.
        status_code = '999'

    return status_code


@app.post("/delete/{fileID}")
def delete_request(fileID: str):
    delete_query = "fileID:%s" % fileID
    delete_cmd = {'delete': {'query': delete_query}}
    r = requests.post(url="http://%s:%s/solr/cdcp/update" % (SOLR_HOST, SOLR_PORT),
                      headers={"content-type": "application/json; charset=UTF-8"},
                      json=delete_cmd)
    return r.status_code
