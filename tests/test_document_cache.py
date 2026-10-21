from agent.document_cache import pdf_pages
from safety.sandbox import Sandbox


def test_cache_shared_across_sessions_moves_and_identical_copies(tmp_path):
    (tmp_path/'a.pdf').write_bytes(b'pdf content')
    called=[]
    def extractor(path,**kw):called.append((path,kw));return ['parsed text']
    def read(name,**kw):return pdf_pages(Sandbox(tmp_path),tmp_path/name,extractor=extractor,**kw)
    assert read('a.pdf')==['parsed text']
    assert read('a.pdf')==['parsed text'] and len(called)==1
    (tmp_path/'a.pdf').rename(tmp_path/'b.pdf')
    assert read('b.pdf')==['parsed text'] and len(called)==1
    read('b.pdf',pages=None); assert len(called)==2  # coverage is part of the key
    (tmp_path/'b.pdf').write_bytes(b'changed content')
    read('b.pdf'); assert len(called)==3


def test_cache_does_not_remember_failures_or_suppress_source_changes(tmp_path):
    import pytest
    path=tmp_path/'a.pdf';path.write_bytes(b'pdf')
    def fail(*args,**kwargs):raise ValueError('parser failed')
    with pytest.raises(ValueError,match='parser failed'):pdf_pages(Sandbox(tmp_path),path,extractor=fail)
    def change(*args,**kwargs):path.write_bytes(b'changed');return ['stale text']
    with pytest.raises(ValueError,match='源文件变化'):pdf_pages(Sandbox(tmp_path),path,extractor=change)
    assert pdf_pages(Sandbox(tmp_path),path,extractor=lambda *a,**k:['fresh'])==['fresh']


def test_unavailable_cache_falls_back_to_normal_read(tmp_path):
    path=tmp_path/'a.pdf';path.write_bytes(b'pdf')
    (tmp_path/'.oplog').write_text('not a folder')
    assert pdf_pages(Sandbox(tmp_path),path,extractor=lambda *a,**k:['text'])==['text']
