## $Id$
## CDSware Search Engine in mod_python.

## This file is part of the CERN Document Server Software (CDSware).
## Copyright (C) 2002 CERN.
##
## The CDSware is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## The CDSware is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.  
##
## You should have received a copy of the GNU General Public License
## along with CDSware; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""CDSware Search Engine in mod_python."""

<protect> ## okay, rest of the Python code goes below #######

__version__ = "$Id$"

## import interesting modules:
try:
    import cgi
    import Cookie
    import cPickle
    import marshal
    import fileinput
    import getopt
    import string
    from string import split
    import os
    import re
    import sys
    import time
    import urllib
    import zlib
    import MySQLdb
    import Numeric
    import md5
    import base64
    import unicodedata
except ImportError, e:
    print "Error: %s" % e
    import sys
    sys.exit(1)    

try:
    from config import *
    from search_engine_config import *
    from dbquery import run_sql
except ImportError, e:
    print "Error: %s" % e
    import sys
    sys.exit(1)

try:
    from webuser import getUid, create_user_infobox
except ImportError, e:
    pass # ignore user personalisation, needed e.g. for command-line
        
search_cache = {} # will cache results of previous searches
dbg = 0 # are we debugging?

## precompile some often-used regexp for speed reasons:
re_a = re.compile('[����]')
re_A = re.compile('[����]')
re_e = re.compile('[����]')
re_E = re.compile('[����]')
re_i = re.compile('[����]')
re_I = re.compile('[����]')
re_o = re.compile('[����]')
re_O = re.compile('[����]')
re_u = re.compile('[����]')
re_U = re.compile('[����]')
re_w = re.compile('[^\w]')
re_logical_and = re.compile('\sand\s')
re_logical_or = re.compile('\sor\s')
re_logical_not = re.compile('\snot\s')
re_operands = re.compile(r'\s([\+\-\|])\s')
re_spaces = re.compile('\s\s+')
re_pattern = re.compile('[^\w\+\*\-\?\|\'\"\,\:\=]')
re_word = re.compile('[\s]')
re_plus = re.compile('\+')
re_minus = re.compile('\-')
re_equal = re.compile('\=')
re_asterisk = re.compile('\*')
re_quote = re.compile('\'')
re_quotes = re.compile('[\'\"]')
re_doublequote = re.compile('\"')
re_subfields = re.compile('\$\$\w')
re_amp = re.compile('&')
re_lt = re.compile('<')

def build_table_latin1_to_ascii():
    """Builds translation table from ISO Latin-1 into ASCII.
    For example, 'f�lin' gets translated into 'felin', etc.
    Suitable for search string pattern replacement."""
    table = range(256)
    for i in table:
        x = unicodedata.decomposition(unichr(i))
        if x and x[0] == "0":
            table[i] = int(x.split()[0], 16)
    return string.join(map(chr, table), "")

# build conversion table:
table_latin1_to_ascii = build_table_latin1_to_ascii()

def get_nicely_ordered_collection_list(collid=1, level=0):
    """Returns nicely ordered (score respected) list of collections, more exactly list of tuples
       (collection name, printable collection name).
       Suitable for create_search_box()."""
    colls_nicely_ordered = []
    query = "SELECT c.name,cc.id_son FROM collection_collection AS cc, collection AS c "\
            " WHERE c.id=cc.id_son AND cc.id_dad='%s' ORDER BY score DESC" % collid
    res = run_sql(query)
    for c, cid in res:
        # make a nice printable name (e.g. truncate c_printable for for long collection names):
        if len(c)>30:
            c_printable = c[:30] + "..."
        else:
            c_printable = c
        if level:
            c_printable = " " + level * '-' + " " + c_printable
        colls_nicely_ordered.append([c, c_printable])
        colls_nicely_ordered  = colls_nicely_ordered + get_nicely_ordered_collection_list(cid, level+1)
    return colls_nicely_ordered

def get_wordsindex_id(field):
    """Returns first words index id where the field code 'field' is word-indexed.
       Returns zero in case there is no words table for this index.
       Example: field='author', output=4."""
    out = 0
    query = """SELECT w.id FROM wordsindex AS w, wordsindex_field AS wf, field AS f
                WHERE f.code='%s' AND wf.id_field=f.id AND w.id=wf.id_wordsindex
                LIMIT 1""" % MySQLdb.escape_string(field)
    res = run_sql(query, None, 1)
    if res:
        out = res[0][0]
    return out

def get_words_from_phrase(phrase, chars_to_treat_as_separators="[\s]"):
    "Returns list of words found in the phrase 'phrase'."
    words = {}
    phrase = asciify_accented_letters(phrase)
    phrase = re.sub(chars_to_treat_as_separators, ' ', phrase) # replace all non-alphabetic characters by spaces
    for word in split(phrase):
        if not words.has_key(word):
            words[word] = 1;
    return words.keys()

def create_opft_search_units(req, p, f, m=None):
    """Splits search pattern and search field into a list of independently searchable units.
       - A search unit consists of '(operand, pattern, field, type)' tuples where
          'operand' is set union (|), set intersection (+) or set exclusion (-);
          'pattern' is either a word (e.g. muon*) or a phrase (e.g. 'nuclear physics');
          'field' is either a code like 'title' or MARC tag like '100__a';
          'type' is the search type ('w' for word file search, 'a' for access file search).
        - Optionally, the function accepts the match type argument 'm'.
          If it is set (e.g. from advanced search interface), then it
          performs this kind of matching.  If it is not set, then a guess is made.
          'm' can have values: 'a'='all of the words', 'o'='any of the words',
                               'p'='phrase/substring', 'r'='regular expression',
                               'e'='exact value'."""

    opfts = [] # will hold (o,p,f,t) units

    ## print input params for debugging purposes:
    if dbg:
        print_warning(req, "p='%s', f='%s'." % (p, f), "Debug (create_opft_search_units())")

    ## check arguments: if matching type phrase/string/regexp, do we have field defined?    
    if (m=='p' or m=='r' or m=='e') and not f:
        m = 'a'        
        print_warning(req, "This matching type cannot be used within <em>any field</em>.  I will perform a word search instead." , "Warning")
        print_warning(req, "If you want to phrase/substring/regexp search in a specific field, e.g. inside title, then please choose <em>within title</em> search option.", "Tip")
        
    ## is desired matching type set?
    if m:
        ## A - matching type is known; good!
        if m == 'e':
            # A1 - exact value:
            opfts.append(['|',p,f,'a']) # '|' since we have only one unit
        elif m == 'p':
            # A2 - phrase/substring:
            opfts.append(['|',"%"+p+"%",f,'a']) # '|' since we have only one unit
        elif m == 'r':
            # A3 - regular expression:
            opfts.append(['|',p,f,'r']) # '|' since we have only one unit
        elif m == 'a':
            # A4 - all of the words:
            for word in get_words_from_phrase(p):
                if len(opfts)==0:
                    opfts.append(['|',word,f,'w']) # '|' in the first unit
                else:
                    opfts.append(['+',word,f,'w']) # '+' in further units
        elif m == 'o':
            # A5 - any of the words:
            for word in get_words_from_phrase(p):
                opfts.append(['|',word,f,'w']) # '|' in all units
        else:
            print_warning(req, "Matching type '%s' is not implemented yet." % m, "Warning")
            opfts.append(['|',"%"+p+"%",f,'a'])            
    else:        
        ## B - matching type is not known: let us try to determine it by some heuristics
        if f and p[0]=='"' and p[-1]=='"':
            ## B0 - does 'p' start and end by double quote, and is 'f' defined? => doing ACC search            
            opfts.append(['|',p[1:-1],f,'a'])
        elif f and p[0]=="'" and p[-1]=="'":
            ## B0bis - does 'p' start and end by single quote, and is 'f' defined? => doing ACC search            
            opfts.append(['|','%'+p[1:-1]+'%',f,'a'])
        elif f and string.find(p, ',') >= 0:
            ## B1 - does 'p' contain comma, and is 'f' defined? => doing ACC search
            opfts.append(['|',p,f,'a'])
        elif f and str(f[0:2]).isdigit():
            ## B2 - does 'f' exist and starts by two digits?  => doing ACC search
            opfts.append(['|',p,f,'a'])            
        else:
            ## B3 - doing WRD search, but maybe ACC too
            # search units are separated by spaces unless the space is within single or double quotes
            # so, let us replace temporarily any space within quotes by '__SPACE__'
            p = re.sub("'(.*?)'", lambda x: "'"+string.replace(x.group(1), ' ', '__SPACE__')+"'", p) 
            p = re.sub("\"(.*?)\"", lambda x: "\""+string.replace(x.group(1), ' ', '__SPACEBIS__')+"\"", p) 
            # wash argument:
            #p = string.strip(re_spaces.sub(' ', re_pattern.sub(' ', p)))
            p = re_equal.sub(":", p)
            p = re_logical_and.sub(" ", p)
            p = re_logical_or.sub(" |", p)
            p = re_logical_not.sub(" -", p)
            p = re_operands.sub(r' \1', p)
            if dbg:
                print_warning(req, "p=%s" % p, "Debug (create_opft_search_units() - B3 started)")
            for pi in split(p): # iterate through separated units (or items, as "pi" stands for "p item")
                pi = re.sub("__SPACE__", " ", pi) # replace back '__SPACE__' by ' ' 
                pi = re.sub("__SPACEBIS__", " ", pi) # replace back '__SPACEBIS__' by ' '
                # firstly, determine set operand
                if pi[0] == '+' or pi[0] == '-' or pi[0] == '|':
                    if len(opfts) or pi[0] == '-': # either not first unit, or '-' for the first unit
                        oi = pi[0]
                    else:
                        oi = "|" # we are in the first unit and operand is not '-', so let us do 
                                 # set union (with still null result set) 
                    pi = pi[1:]
                else:
                    # okay, there is no operand, so let us decide what to do by default
                    if len(opfts):
                        oi = '+' # by default we are doing set intersection...
                    else:
                        oi = "|" # ...unless we are in the first unit
                # secondly, determine search pattern and field:
                if string.find(pi, ":") > 0:
                    fi, pi = split(pi, ":", 1)
                else:
                    fi, pi = f, pi
                # look also for old ALEPH field names:
                if fi and cfg_fields_convert.has_key(string.lower(fi)):
                    fi = cfg_fields_convert[string.lower(fi)]
                # wash 'pi' argument:
                if re_quotes.match(pi):
                    # B3a - quotes are found => do ACC search (phrase search)
                    if fi:
                        if re_doublequote.match(pi):
                            pi = string.replace(pi, '"', '') # get rid of quotes
                            opfts.append([oi,pi,fi,'a'])
                        else:
                            pi = string.replace(pi, "'", '') # get rid of quotes
                            opfts.append([oi,"%"+pi+"%",fi,'a'])
                    else:
                        # fi is not defined, look at where we are doing exact or subphrase search (single/double quotes):
                        if pi[0]=='"' and pi[-1]=='"':                        
                            opfts.append([oi,pi[1:-1],"anyfield",'a'])
                            print_warning(req, "Searching for an exact match inside any field may be slow.  You may want to try to search for words instead, or choose a search within specific field.", "Warning")
                        else:                        
                            # nope, subphrase in global index is not possible => change back to WRD search
                            for pii in get_words_from_phrase(pi):
                                # since there may be '-' and other chars that we do not index in WRD
                                opfts.append([oi,pii,fi,'w'])
                            print_warning(req, "The sub-phrase search does not work in any field.  I'll do a 'logical AND' style of search instead.", "Warning")
                            print_warning(req, "If you want to do a sub-phrase search in a specific field, e.g. inside title, then please choose 'within title' search option.", "Tip")
                            print_warning(req, "If you want to do exact phrase matching, then please use double quotes.", "Tip")
                elif fi and str(fi[0]).isdigit() and str(fi[0]).isdigit():
                    # B3b - fi exists and starts by two digits => do ACC search
                    opfts.append([oi,pi,fi,'a'])            
                elif fi and not get_wordsindex_id(fi):
                    # B3c - fi exists but there is no words table for fi => try ACC search
                    opfts.append([oi,pi,fi,'a'])            
                else:
                    # B3d - general case => do WRD search                    
                    for pii in get_words_from_phrase(pi): 
                        opfts.append([oi,pii,fi,'w'])

    ## sanity check:
    for i in range(0,len(opfts)):
        pi = opfts[i][1]
        if pi == '*':
            print_warning(req, "Ignoring standalone wildcard word.", "Warning")
            del opfts[i]

    ## return search units:
    if dbg:
        for opft in opfts:
            print_warning(req, opft, "Debug (create_opft_search_units() - created search unit)")            
    return opfts

def create_header(cc=cdsname, as=0, headeradd=""):
    "Creates CDS header and info on URL and date."
    return """
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html>
<head>
  <title>%s - Search Results</title>
  <link rev="made" href="mailto:%s">
  <link rel="stylesheet" href="%s/img/cds.css">
  <meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">
</head>
<body>
<div class="pageheader">%s</div>
%s
%s
""" % (cc, supportemail, weburl,
       cdspageheader, \
       create_navtrail(cc, as,
                       """<table border="0" cellspacing="0" cellpadding="2"><tr class="navtrail"><td><small><small>""",
                       "","&gt;","", "&gt; Search Results</small></small></td></tr></table>",0), \
       headeradd)

def create_inputdate_box(title="From date", name="d1", selected_year="", selected_month="", selected_day=""):
    "Produces 'From Date', 'Until Date' kind of selection box.  Suitable for search options."
    box = ""
    box += "<small><strong>%s:</strong></small><br>" % title
    # day
    box += """<select name="%sd">""" % name
    box += """<option value="">any day"""
    for day in range(1,32):
        box += """<option value="%02d"%s>%02d""" % (day, is_selected(day, selected_day), day)
    box += """</select>"""
    # month
    box += """<select name="%sm">""" % name
    box += """<option value="">any month"""
    for mm, month in [('01','January'), ('02','February'), ('03','March'), ('04','April'), \
                      ('05','May'), ('06','June'), ('07','July'), ('08','August'), \
                      ('09','September'), ('10','October'), ('11','November'), ('12','December')]:
        box += """<option value="%s"%s>%s""" % (mm, is_selected(mm, selected_month), month)
    box += """</select>"""
    # year
    box += """<select name="%sy">""" % name
    box += """<option value="">any year"""
    for year in range(1980,2004):
        box += """<option value="%d"%s>%d""" % (year, is_selected(year, selected_year), year)
    box += """</select>"""        
    return box

def create_search_box(cc, colls, p, f, rg, sf, so, sp, of, ot, as, p1, f1, m1, op1, p2, f2, m2, op2, p3, f3, m3, sc,
                      d1y, d1m, d1d, d2y, d2m, d2d):
    "Create search box for 'search again in the results page' functionality."
    out = ""
    # print search box prolog:
    out += """
    <form action="%s/search.py" method="get">
    <strong class="headline"><span class="h1">%s</span></strong>
    <input type="hidden" name="cc" value="%s"> 
    <input type="hidden" name="as" value="%s">
    """ % (weburl, cc, cc, as)
    if ot:
        out += """<input type="hidden" name="ot" value="%s">""" % ot
    if sp:
        out += """<input type="hidden" name="sp" value="%s">""" % sp 

    # possibly print external search engines links (Google box):
    if cfg_google_box:
        out += """<table align="right"><tr><td>%s</td></tr></table>""" % create_google_box(p, f, p1, p2, p3)
    out += "<table>"
    # firstly, print Query box:
    if as==1:
        # print Advanced Search form:
        # define search box elements:
        cell_1_left = "<small><strong>Search for:</strong></small><br>" + create_matchtype_box('m1', m1) + \
                      """<input type="text" name="p1" size="%d" value="%s">""" % (cfg_advancedsearch_pattern_box_width, cgi.escape(p1,1))
        cell_1_right = "<small><strong>within:</strong></small><br>%s" % create_searchwithin_selection_box('f1', f1)
        cell_1_moreright = create_andornot_box('op1', op1)
        cell_1_farright = ""
        cell_2_left = create_matchtype_box('m2', m2) + """<input type="text" name="p2" size="%d" value="%s">""" % (cfg_advancedsearch_pattern_box_width, cgi.escape(p2,1))
        cell_2_right = create_searchwithin_selection_box('f2', f2)
        cell_2_moreright = create_andornot_box('op2', op2)
        cell_2_farright = ""
        cell_3_left = create_matchtype_box('m3', m3) + """<input type="text" name="p3" size="%d" value="%s">""" % (cfg_advancedsearch_pattern_box_width, cgi.escape(p3,1))
        cell_3_right = create_searchwithin_selection_box('f3', f3)
        cell_3_moreright = """<input type="submit" name="search" value="SEARCH"><input type="submit" name="search" value="Browse">&nbsp;"""
        cell_3_farright = """<small class="light"><a href="%s/search.py?p=%s&amp;f=%s&amp;cc=%s">Simple&nbsp;Search</a></small>
                             <br><small><a href="%s/help/search/tips.html">Search&nbsp;Tips</a></small>""" % \
                             (weburl, urllib.quote(p1), urllib.quote(f1), urllib.quote(cc), weburl)
        # print them:
        out += """
        <tr>
        <td valign="top" nowrap>
         <table>
         <tr valign="bottom">
           <td>%s</td>
           <td>%s</td>
           <td>%s</td>
           <td>%s</td>
         </tr>
         <tr valign="bottom">
           <td>%s</td>
           <td>%s</td>
           <td>%s</td>
           <td>%s</td>
         </tr>
         <tr valign="bottom">
           <td>%s</td>
           <td>%s</td>
           <td>%s</td>
           <td>%s</td>
         </tr>
         </table>
        </td>
        </tr>""" % \
         (cell_1_left, cell_1_right, cell_1_moreright, cell_1_farright, \
          cell_2_left, cell_2_right, cell_2_moreright, cell_2_farright, \
          cell_3_left, cell_3_right, cell_3_moreright, cell_3_farright)         
    else:
        # print Simple Search form:
        cell_1_left = """<small><strong>Search for:</strong></small>
                         <br><input type="text" name="p" size="%d" value="%s">""" % \
        (cfg_simplesearch_pattern_box_width, cgi.escape(p, 1))
        cell_1_middle = "<small><strong>within:</strong></small><br>%s" % create_searchwithin_selection_box('f', f)
        cell_1_right = """<input type="submit" name="search" value="SEARCH"><input type="submit" name="search" value="Browse">&nbsp;"""
        cell_1_farright = """<small class="light"><a href="%s/search.py?p1=%s&amp;f1=%s&amp;as=1&amp;cc=%s">Advanced&nbsp;Search</a></small>
                             <br><small><a href="%s/help/search/tips.html">Search&nbsp;Tips</a></small>""" %\
                          (weburl, urllib.quote(p), urllib.quote(f), urllib.quote(cc), weburl)
        out += """
        <tr>
        <td valign="top" nowrap>
         <table>
         <tr valign="bottom">
           <td>%s</td>
           <td>%s</td>
           <td>%s</td>
           <td>%s</td>
         </tr>
         </table>
        </td>
        </tr>
        """ % (cell_1_left, cell_1_middle, cell_1_right, cell_1_farright)
    # secondly, print Collection(s) box:
    out += """
    <tr>
    <td valign="top">
    <small><strong>Search collections:</strong></small><br>"""
    colls_nicely_ordered = get_nicely_ordered_collection_list()    
    if colls and colls[0] != cdsname:
        # some collections are defined, so print these first, and only then print 'add another collection' heading:
        for c in colls:
            if c:
                out += """<select name="c"><option value="">*** remove this collection ***"""
                for (cx, cx_printable) in colls_nicely_ordered:
                    # print collection:
                    if not cx.startswith("Unnamed collection"):                    
                        out+= """<option value="%s"%s>%s""" % (cx, is_selected(c, re.sub("^[\s\-]*","",cx)), cx_printable)
                out += """</select>"""
        out += """<select name="c"><option value="">*** add another collection ***"""
    else: # we searched in CDSNAME, so print 'any collection' heading
        out += """<select name="c"><option value="">*** any collection ***"""
    for (cx, cx_printable) in colls_nicely_ordered:
        if not cx.startswith("Unnamed collection"):
            out += """<option value="%s">%s""" % (cx, cx_printable)
    out += """
    </select>
    </td>
    </tr>"""
    # thirdly, print from/until date boxen, if applicable:
    if d1y=="" and d1m=="" and d1d=="" and d2y=="" and d2m=="" and d2d=="":
        pass # do not need it
    else:
        cell_6_a = create_inputdate_box("Added since", "d1", d1y, d1m, d1d)
        cell_6_b = create_inputdate_box("until", "d2", d2y, d2m, d2d)
        out += """<table cellpadding="3" cellspacing="0">
                    <tr>
                      <td colspan="3" height="3">
                      </td>
                    </tr>
                    <tr valign="bottom">
                      <td>%s</td>
                      <td>%s</td>
                    </tr>
                   </table>""" % \
           (cell_6_a, cell_6_b)        
    # fourthly, print Display/Sort box:
    cell_1_left = """<small><strong>Sort by:</strong></small><br>
    <select name="sf">
    <option value="">- latest first -"""
    query = """SELECT DISTINCT(f.code),f.name FROM field AS f, collection_field_fieldvalue AS cff
                WHERE cff.type='soo' AND cff.id_field=f.id
                ORDER BY cff.score DESC, f.name ASC""" 
    res = run_sql(query)
    for code, name in res:
        # propose found sort options:
        cell_1_left += """<option value="%s"%s>%s""" % (code, is_selected(sf,code), name)
    cell_1_left += """</select>"""
    cell_1_left += """<select name="so">
                      <option value="a"%s>asc.
                      <option value="d"%s>desc.
                      </select>""" % (is_selected(so,"a"),is_selected(so,"d"))
    cell_1_right = """<small><strong>Output format:</strong></small><br>
    <select name="of">"""
    query = """SELECT code,name FROM format ORDER BY name ASC""" 
    res = run_sql(query)
    if res:
        # propose found formats:
        for code, name in res:
            cell_1_right += """<option value="%s"%s>%s""" % (code, is_selected(of,code), name)
    else:
        # no formats are found, so propose the default HTML one:
        cell_1_right += """<option value="hb"%s>HTML brief""" % (is_selected(of,"hb"))
    # is format made of numbers only? if yes, then propose it too:
    if of and str(of[0:3]).isdigit():
        cell_1_right += """<option value="%s" selected>%s MARC tag""" % (of, of)
    cell_1_right += """</select>"""
    ## okay, formats ended
    cell_1_middle = """<small><strong>Display results:</strong></small><br>
    <select name="rg">
    <option value="10"%s>10 results
    <option value="25"%s>25 results
    <option value="50"%s>50 results
    <option value="100"%s>100 results
    <option value="250"%s>250 results
    <option value="500"%s>500 results
    </select>
    <select name="sc">
    <option value="0"%s>grouped together 
    <option value="1"%s>split by collection
    </select>
    """ % (is_selected(rg,"10"), is_selected(rg,"25"), is_selected(rg,"50"), \
           is_selected(rg,"100"), is_selected(rg,"250"), is_selected(rg,"500"),\
           is_selected(sc,"0"), is_selected(sc,"1"))
    out += """
    <tr>
    <td valign="top">
    <table><tr valign="top"><td>%s</td><td>%s</td><td>%s</td></tr></table>
    </td>
    </tr>""" % (cell_1_left, cell_1_middle, cell_1_right)
    # print end of search box:
    out += """</table></form>
    """
    return out

def nice_number(num):
    "Returns nice number when using comma as thousands separator."
    chars_in = list(str(num))
    num = len(chars_in)
    chars_out = []
    for i in range(0,num):
        if i % 3 == 0 and i != 0:
            chars_out.append(',')
        chars_out.append(chars_in[num-i-1])
    chars_out.reverse()
    return ''.join(chars_out)

def is_selected(var, fld):
    "Checks if the two are equal, and if yes, returns ' selected'.  Useful for select boxes."
    if str(var) == str(fld):
        return " selected"
    elif fld and len(fld)==3 and fld[0] == "w" and var == fld[1:]:
        return " selected"
    else:
        return ""

def create_navtrail(cc=cdsname,
                    as=0,
                    header="""<table width="100%" border="0" cellspacing="0" cellpadding="0"><tr class="pageboxlefttop"><td><small>""", \
                    prolog="", \
                    separator="&gt;", \
                    epilog="", \
                    footer="&gt; Search Results</small></td></tr></table>",
                    exclude_root=0):
    """Creates navigation trail, i.e. links to ancestors of the 'cc' collection.
    If as==1, then links to Advanced Search interfaces; otherwise Simple Search.
    """
    # firstly, display navtrail prologue:
    navtrail = header        
    # first, display list of ancestors:
    for dad in get_coll_ancestors(cc):
        if dad != cdsname:
            navtrail += """%s <a class="navtrail" href="%s/?c=%s&as=%d">%s</a> %s %s """ % \
                        (prolog, weburl, urllib.quote_plus(dad), as, dad, epilog, separator)
        else: # hide cdsname for aesthetical reasons
            navtrail += """%s <a class="navtrail" href="%s/?as=%d">%s</a> %s %s """ % \
                        (prolog, weburl, as, dad, epilog, separator)            
    # then print cc:
    if cc != cdsname or not exclude_root:
        navtrail += """%s <a class="navtrail" href="%s/?c=%s&as=%d">%s</a> %s""" % (prolog, weburl, urllib.quote_plus(cc), as, cc, epilog)
    # last, print navtrail epilogue:
    navtrail += footer
    return navtrail

def create_searchwithin_selection_box(fieldname='f', value=''):
    "Produces 'search within' selection box for the current collection."
    out = ""
    out += """<select name="%s">""" % fieldname
    out += """<option value="">any field"""
    query = "SELECT code,name FROM field ORDER BY name ASC"
    res = run_sql(query)
    for field_code, field_name in res:
        if field_code and field_code != "anyfield":
            out += """<option value="%s"%s>%s""" % (field_code, is_selected(field_code,value), field_name)
    if value and str(value[0]).isdigit():
        out += """<option value="%s" selected>%s MARC tag""" % (value, value)
    out += """</select>""" 
    return out

def create_andornot_box(name='op', value=''):
    "Returns HTML code for the AND/OR/NOT selection box."
    out = """
    <select name="%s">
    <option value="a"%s>AND
    <option value="o"%s>OR
    <option value="n"%s>AND NOT
    </select>
    """ % (name, is_selected('a', value), is_selected('o', value), is_selected('n', value))
    return out

def create_matchtype_box(name='m', value=''):
    "Returns HTML code for the 'match type' selection box."
    out = """
    <select name="%s">
    <option value="a"%s>All of the words:
    <option value="o"%s>Any of the words:
    <option value="p"%s>Phrase/substring:
    <option value="r"%s>Regular expression:
    <option value="e"%s>Exact value:
    </select>
    """ % (name, is_selected('a', value), is_selected('o', value), is_selected('p', value), 
                 is_selected('r', value), is_selected('e', value))
    return out

def create_google_box(p, f, p1, p2, p3,
                      prolog="""<table border=0 cellpadding=0 cellspacing=0><tr><td width="30"></td><td class="pageboxrighttopadd"><small><strong>Try your search on:</strong><br> """,
                      separator= """<br>""",
                      epilog="""</small></td></tr></table>"""):
    "Creates the box that proposes links to other useful search engines like Google.  'p' is the search pattern."
    out = ""
    if not p and (p1 or p2 or p3):
        p = p1 + " " + p2 + " " + p3 
    if cfg_google_box == 1:
        out += prolog
        if cfg_google_box_cern:
            # CERN Intranet:
            out += """<a href="http://search.cern.ch/query.html?qt=%s">CERN&nbsp;Intranet</a>""" % urllib.quote(p)
            # SPIRES
            if f == "author":
                out += separator
                out += """<a href="http://www.slac.stanford.edu/spires/find/hep/www?AUTHOR=%s">SPIRES</a>""" % urllib.quote(p)
            elif f == "title":
                out += separator
                out += """<a href="http://www.slac.stanford.edu/spires/find/hep/www?TITLE=%s">SPIRES</a>""" % urllib.quote(p)
            elif f == "reportnumber":
                out += separator
                out += """<a href="http://www.slac.stanford.edu/spires/find/hep/www?REPORT-NUM=%s">SPIRES</a>""" % urllib.quote(p)
            elif f == "keyword":
                out += separator
                out += """<a href="http://www.slac.stanford.edu/spires/find/hep/www?k=%s">SPIRES</a>""" % urllib.quote(p)
            # KEK
            if f == "author":
                out += separator
                out += """<a href="http://www-lib.kek.jp/cgi-bin/kiss_prepri?AU=%s">KEK</a>""" % urllib.quote(p)        
            elif f == "title":
                out += separator
                out += """<a href="http://www-lib.kek.jp/cgi-bin/kiss_prepri?TI=%s">KEK</a>""" % urllib.quote(p)        
            elif f == "reportnumber":
                out += separator
                out += """<a href="http://www-lib.kek.jp/cgi-bin/kiss_prepri?RP=%s">KEK</a>""" % urllib.quote(p)        
            out += separator
        # Google:
        out += """<a href="http://google.com/search?q=%s">Google</a>""" % urllib.quote(p)
        # AllTheWeb:
        out += separator
        out += """<a href="http://alltheweb.com/search?q=%s">AllTheWeb</a>""" % urllib.quote(p)
        out += epilog
    return out

def create_footer():    
    "Creates CDS page footer."
    return cdspagefooter
     
class HitList:
    """Class describing set of records, implemented as bit vectors of recIDs.
    Using Numeric arrays for speed (1 value = 8 bits), can use later "real"
    bit vectors to save space."""

    def __init__(self, anArray=None):
        self._nbhits = -1
        if anArray:
            self._set = anArray
        else:
            self._set = Numeric.zeros(cfg_max_recID+1, Numeric.Int0)

    def __repr__(self, join=string.join):
        return "%s(%s)" % (self.__class__.__name__, join(map(repr, self._set), ', '))

    def add(self, recID):
        "Adds a record to the set."
        self._set[recID] = 1

    def addmany(self, recIDs):
        "Adds several recIDs to the set."
        for recID in recIDs: self._set[recID] = 1

    def addlist(self, arr):
        "Adds an array of recIDs to the set."
        Numeric.put(self._set, arr, 1)

    def remove(self, recID):
        "Removes a record from the set."
        self._set[recID] = 0

    def removemany(self, recIDs):
        "Removes several records from the set."
        for recID in recIDs:
            self.remove(recID)

    def intersect(self, other):
        "Does a set intersection with other.  Keep result in self."
        self._set = Numeric.bitwise_and(self._set, other._set)

    def union(self, other):
        "Does a set union with other. Keep result in self."
        self._set = Numeric.bitwise_or(self._set, other._set)

    def difference(self, other):
        "Does a set difference with other. Keep result in self."
        #self._set = Numeric.bitwise_not(self._set, other._set)
        for recID in Numeric.nonzero(other._set):
            self.remove(recID)

    def contains(self, recID):
        "Checks whether the set contains recID."
        return self._set[recID]

    __contains__ = contains     # Higher performance member-test for python 2.0 and above

    def __getitem__(self, index):
        "Support for the 'for item in set:' protocol."
        return Numeric.nonzero(self._set)[index]
        
    def calculate_nbhits(self):
        "Calculates the number of records set in the hitlist."
        self._nbhits = Numeric.sum(self._set.copy().astype(Numeric.Int))

    def items(self):
        "Return an array containing all recID."
        return Numeric.nonzero(self._set)

# speed up HitList operations by ~20% if Psyco is installed:
try:
    import psyco
    psyco.bind(HitList)
except:
    pass

def escape_string(s):
    "Escapes special chars in string.  For MySQL queries."
    s = MySQLdb.escape_string(s)
    return s

def asciify_accented_letters(s):
    "Translates ISO-8859-1 accented letters into their ASCII equivalents."
    s = string.translate(s, table_latin1_to_ascii)
    return s

def wash_colls(cc, c, split_colls=0):

    """Wash collection list by checking whether user has deselected
    anything under 'Narrow search'.  Checks also if cc is a list or not.
       Return list of cc, colls_to_display, colls_to_search since the list
    of collections to display is different from that to search in.
    This is because users might have chosen 'split by collection'
    functionality.
       The behaviour of "collections to display" depends solely whether

    user has deselected a particular collection: e.g. if it started
    from 'Articles and Preprints' page, and deselected 'Preprints',
    then collection to display is 'Articles'.  If he did not deselect
    anything, then collection to display is 'Articles & Preprints'.
       The behaviour of "collections to search in" depends on the
    'split_colls' parameter:
         * if is equal to 0, then we can wash the colls list down
           and search solely in the collection the user started from;
         * if is equal to 1, then we are splitting to the first level
           of collections, i.e. collections as they appear on the page
           we started to search from;
         * if it is equal to 9, then we are splitting right to the
           bottom level.
    """
       
    colls_out = []
    colls_out_for_display = []

    # check what type is 'cc':
    if type(cc) is list:
        for ci in cc:
            if collrecs_cache.has_key(ci):
                # yes this collection is real, so use it:
                cc = ci
                break
    else:
        # check once if cc is real:
        if not collrecs_cache.has_key(cc):
            cc = cdsname # cc is not real, so replace it with Home collection

    # check type of 'c' argument:
    if type(c) is list:
        colls = c
    else:
        colls = [c]

    # remove all 'unreal' collections:
    colls_real = []
    for coll in colls:
        if collrecs_cache.has_key(coll):
            colls_real.append(coll)
    colls = colls_real

    # check if some real collections remain:
    if len(colls)==0:
        colls = [cc]

    # then let us check the list of non-restricted "real" sons of 'cc' and compare it to 'coll':
    query = "SELECT c.name FROM collection AS c, collection_collection AS cc, collection AS ccc WHERE c.id=cc.id_son AND cc.id_dad=ccc.id AND ccc.name='%s' AND cc.type='r' AND c.restricted IS NULL" % MySQLdb.escape_string(cc)
    res = run_sql(query)
    l_cc_nonrestricted_sons = []
    l_c = colls
    for row in res:
        l_cc_nonrestricted_sons.append(row[0]) 
    l_c.sort()
    l_cc_nonrestricted_sons.sort()
    if l_cc_nonrestricted_sons == l_c:
        colls_out_for_display = [cc] # yep, washing permitted, it is sufficient to display 'cc'
    else:
        colls_out_for_display = colls # nope, we need to display all 'colls' successively

    # remove duplicates:
    colls_out_for_display_nondups=filter(lambda x, colls_out_for_display=colls_out_for_display: colls_out_for_display[x-1] not in colls_out_for_display[x:], range(1, len(colls_out_for_display)+1))
    colls_out_for_display = map(lambda x, colls_out_for_display=colls_out_for_display:colls_out_for_display[x-1], colls_out_for_display_nondups)
        
    # second, let us decide on collection splitting:
    if split_colls == 0:
        # type A - no sons are wanted
        colls_out = colls_out_for_display
#    elif split_colls == 1:
    else:
        # type B - sons (first-level descendants) are wanted
        for coll in colls_out_for_display:
            coll_sons = get_coll_sons(coll)
            if coll_sons == []:
                colls_out.append(coll)
            else:
                colls_out = colls_out + coll_sons
# TODO: enable sc=9 back one day
#    else:
#        # type C - we are splitting right to the bottom level.
#        # This decomposes compound collections like "A & B" into a list
#        # of "A" and "B", provided that "A & B" has no associated
#        # dbcollid collection indicators defined, and that both "A" and
#        # "B" have defined some dbcollids.
#        for coll in colls_out_for_display:
#            coll_real_descendants = get_coll_real_descendants(coll)
#           if coll_real_descendants:
#                colls_out.extend(coll_real_descendants)
#            else:
#                colls_out.append(coll)

    # remove duplicates:
    colls_out_nondups=filter(lambda x, colls_out=colls_out: colls_out[x-1] not in colls_out[x:], range(1, len(colls_out)+1))
    colls_out = map(lambda x, colls_out=colls_out:colls_out[x-1], colls_out_nondups)

    # fill collrecs_cache for wanted collection, if not already filled:
    for coll in colls_out:
        if not collrecs_cache[coll]:
            collrecs_cache[coll] = get_collection_hitlist(coll)

    return (cc, colls_out_for_display, colls_out)
 
def wash_pattern(p):
    """Wash pattern passed by URL."""
    # check whether p is a list:
    if type(p) is list:
        for pi in p:
            if pi:
                p = pi
                break
    # add leading/trailing whitespace for the two following wildcard-sanity checking regexps:
    p = " " + p + " " 
    # get rid of wildcards at the beginning of words:
    p = re.sub(r'(\s)[\*\%]+', "\\1", p)
    # get rid of extremely short words (1-3 letters with wildcards): TODO: put into the search config
    p = re.sub(r'(\s\w{1,3})[\*\%]+', "\\1", p)
    # remove unnecessary whitespace:
    p = string.strip(p)
    return p
    
def wash_field(f):
    """Wash field passed by URL."""
    # check whether f is a list:
    if type(f) is list:
        for fi in f:
            if fi:
                f = fi
                break
    # get rid of unnecessary whitespace:
    f = string.strip(f)
    # wash old-style CDSware/ALEPH 'f' field argument, e.g. replaces 'wau' and 'au' by 'author'
    if cfg_fields_convert.has_key(string.lower(f)):
        f = cfg_fields_convert[f]
    return f

def wash_dates(d1y, d1m, d1d, d2y, d2m, d2d):
    """Take user-submitted dates (day, month, year) of the web form and return (day1, day2) in YYYY-MM-DD format
    suitable for time restricted searching.  I.e. pay attention when months are not there to put 01 or 12
    according to if it's the starting or the ending date, etc."""    
    day1, day2 =  "", ""
    # sanity checking:
    if d1y=="" and d1m=="" and d1d=="" and d2y=="" and d2m=="" and d2d=="":
        return ("", "") # nothing selected, so return empty values
    # construct day1 (from):
    if d1y:
        day1 += "%04d" % int(d1y)
    else:
        day1 += "0000"
    if d1m:
        day1 += "-%02d" % int(d1m)
    else:
        day1 += "-01"
    if d1d:
        day1 += "-%02d" % int(d1d)
    else:
        day1 += "-01"
    # construct day2 (until):
    if d2y:
        day2 += "%04d" % int(d2y)
    else:
        day2 += "9999"
    if d2m:
        day2 += "-%02d" % int(d2m)
    else:
        day2 += "-12"
    if d2d:
        day2 += "-%02d" % int(d2d)
    else:
        day2 += "-31" # NOTE: perhaps we should add max(datenumber) in
                      # given month, but for our quering it's not
                      # needed, 31 will always do
    # okay, return constructed YYYY-MM-DD dates
    return (day1, day2)
    
def get_coll_ancestors(coll):
    "Returns a list of ancestors for collection 'coll'."
    coll_ancestors = [] 
    coll_ancestor = coll
    while 1:
        query = "SELECT c.name FROM collection AS c "\
                "LEFT JOIN collection_collection AS cc ON c.id=cc.id_dad "\
                "LEFT JOIN collection AS ccc ON ccc.id=cc.id_son "\
                "WHERE ccc.name='%s' ORDER BY cc.id_dad ASC LIMIT 1" \
                % escape_string(coll_ancestor)
        res = run_sql(query, None, 1)        
        if res:
            coll_name = res[0][0]
            coll_ancestors.append(coll_name)
            coll_ancestor = coll_name
        else:
            break
    # ancestors found, return reversed list:
    coll_ancestors.reverse()
    return coll_ancestors

def get_coll_sons(coll):
    "Returns a list of sons (first-level descendants) for collection 'coll'."
    coll_sons = [] 
    query = "SELECT c.name FROM collection AS c "\
            "LEFT JOIN collection_collection AS cc ON c.id=cc.id_son "\
            "LEFT JOIN collection AS ccc ON ccc.id=cc.id_dad "\
            "WHERE ccc.name='%s' ORDER BY cc.score DESC" \
            % escape_string(coll)
    res = run_sql(query)
    for name in res:
        coll_sons.append(name[0])
    return coll_sons

def get_coll_real_descendants(coll):
    """Return a list of all descendants of collection 'coll' that are defined by a 'dbquery'.
       IOW, we need to decompose compound collections like "A & B" into "A" and "B" provided
       that "A & B" has no associated database query defined.
    """
    coll_sons = [] 
    query = "SELECT c.name,c.dbquery FROM collection AS c "\
            "LEFT JOIN collection_collection AS cc ON c.id=cc.id_son "\
            "LEFT JOIN collection AS ccc ON ccc.id=cc.id_dad "\
            "WHERE ccc.name='%s' ORDER BY cc.score DESC" \
            % escape_string(coll)
    res = run_sql(query)
    for name, dbquery in res:
        if dbquery: # this is 'real' collection, so return it:
            coll_sons.append(name)
        else: # this is 'composed' collection, so recurse:
            coll_sons.extend(get_coll_real_descendants(name))
    return coll_sons

def get_collection_hitlist(coll):
    """Return bit vector of records that belong to the collection 'coll'."""
    set = HitList()
    query = "SELECT nbrecs,reclist FROM collection WHERE name='%s'" % coll
    # launch the query:
    res = run_sql(query, None, 1)
    # fill the result set:
    if res:
        try:
            set._nbhits, set._set = res[0][0], Numeric.loads(zlib.decompress(res[0][1]))
        except:
            set._nbhits = 0
    # okay, return result set:
    return set

def coll_restricted_p(coll):
    "Predicate to test if the collection coll is restricted or not."
    if not coll:
        return 0
    query = "SELECT restricted FROM collection WHERE name='%s'" % MySQLdb.escape_string(coll)
    res = run_sql(query, None, 1)
    if res and res[0][0] != None:       
        return 1
    else:
        return 0

def coll_restricted_group(coll):
    "Return Apache group to which the collection is restricted.  Return None if it's public."
    if not coll:
        return None
    query = "SELECT restricted FROM collection WHERE name='%s'" % MySQLdb.escape_string(coll)
    res = run_sql(query, None, 1)
    if res:
        return res[0][0]
    else:
        return None
    
def create_collrecs_cache():
    """Creates list of records belonging to collections.  Called on startup
    and used later for intersecting search results with collection universe."""
    collrecs = {}
    query = "SELECT name FROM collection"
    res = run_sql(query)
    for name in res:
        collrecs[name[0]] = None # this will be filled later during runtime by calling get_collection_hitlist(coll)        
    return collrecs

try:
    collrecs_cache.has_key(cdsname)
except:
    collrecs_cache = create_collrecs_cache()
    collrecs_cache[cdsname] = get_collection_hitlist(cdsname)

def browse(req, colls, p, f, rg):
    """Browse either biliographic phrases or words indexes."""
    return

def browse_in_bibwords(p, f):
    """Browse inside words indexes."""
    return

def browse_in_bibxxx(req, colls, p, f, rg):
    """Browse bibliographic phrases for the given pattern in the given field."""
    print_warning(req, "The browse functionality is about to be expanded and prettyfied.", "Warning")
    ## determine browse field:
    if string.find(p, ":") > 0: # does 'p' contain ':'?
        f, p = split(p, ":", 2)
    ## check arguments:
    if not f:
        print_warning(req, "Sorry, cannot browse within any field.  Please choose a field to browse in (e.g. <em>within title</em>).",
                      "Error")
        return 1
    ## wash 'p' argument:
    p = re_quotes.sub("", p)
    ## construct 'tl' which defines the tag list (MARC tags) to search in:
    tl = []
    if str(f[0]).isdigit() and str(f[1]).isdigit():
        tl.append(f) # 'f' seems to be okay as it starts by two digits
    else:
        # deduce desired MARC tags on the basis of chosen 'f'
        tl = get_field_tags(f)
        if not tl:
            # by default we are searching in author index:
            tl = get_field_tags("author")
            print_warning(req, "The browse does not work in this field.  Choosing author index instead.", "Warning")
    ## okay, start browse:
    for t in tl:
        req.write("<p><strong>Browsing in '%s' field:</strong>" % t)
        # deduce into which bibxxx table we will search:
        digit1, digit2 = int(t[0]), int(t[1])
        bx = "bib%d%dx" % (digit1, digit2)
        bibx = "bibrec_bib%d%dx" % (digit1, digit2)
        # construct query:
        if len(t) != 6 or t[-1:]=='%': # only the beginning of field 't' is defined, so add wildcard character:
            query = "SELECT bx.id, bx.value FROM %s AS bx WHERE bx.value >= '%s' AND bx.tag LIKE '%s%%' ORDER BY bx.value ASC LIMIT %d" \
                    % (bx, p, t, rg)
        else:
            query = "SELECT bx.id, bx.value FROM %s AS bx WHERE bx.value >= '%s' AND bx.tag='%s' ORDER BY bx.value ASC LIMIT %d" \
                    % (bx, p, t, rg)
        # launch the query:
        res = run_sql(query)
        # display results:
        for row in res:
            bx_id, bx_val = row[0], row[1]
            # deduce number of hits (=different RECIDs) for 'bx_val':
            query_bis = "SELECT COUNT(DISTINCT(bibx.id_bibrec)) FROM %s AS bibx WHERE bibx.id_bibxxx='%s'" % (bibx, bx_id)
            query_bis_hits = 0
            res_bis = run_sql(query_bis, None, 1)
            if res_bis:
                query_bis_hits = res_bis[0][0]
            hits = "%d record" % query_bis_hits
            if query_bis_hits > 1:
                hits = hits + "s"
            req.write("""<br>&nbsp;&nbsp;&nbsp;%s ... <a href="%s/search.py?p=%s&f=%s">%s</a>""" % (bx_val, weburl, bx_val, t, hits))
    return 

def search_pattern(req, p=None, f=None, colls=None, m=None, hit_hints=0):
    """Searches for pattern 'p' and field 'f' and returns dict of recIDs HitLists per each collection in 'colls'.
    - Optionally, the function accepts the match type argument 'm'.
      If it is set (e.g. from advanced search interface), then it
      performs this kind of matching.  If it is not set, then a guess
      is made.      
    - Calls search_in_bibwords() and/or search_in_bibxxx() functions.
    - If hit_hints is set, than print lots of hints on current search.  Useful for debugging and/or when search gives zero hits.
    - Called by main()."""    

    ## create result set:
    results = HitList()
    results_used = 0 # not-yet-used flag, to be able to circumvent set operations

    ## if p is not defined, return all hits in given collections:
    if not p:
        results_out = {}
        for coll in colls:
            results_out[coll] = HitList()
            results_out[coll]._set = collrecs_cache[coll]._set
            results_out[coll]._nbhits = collrecs_cache[coll]._nbhits
        return results_out

    ## now we are sure to have some p: good.

    ## create search units:
    opft_items = create_opft_search_units(req, p, f, m)
    #req.write("<p>%s" % opft_items)
    
    hit_hints_displayed = 0 # did we already displayed some search hint?
    
    ## search regardless of collection:
    for opft_item in opft_items:
        results_for_opft_item = {}
        oi, pi, fi, ti = opft_item[0], opft_item[1], opft_item[2], opft_item[3]
        # firstly, launch search for this pattern item:
        if ti == 'a' or ti == 'r': # we are doing either direct bibxxx search or phrase search or regexp search
            results_for_opft_item = search_in_bibxxx(req, pi, fi, ti)
        elif ti == 'w': # we are doing bibwords search
            results_for_opft_item = search_in_bibwords(req, pi, fi)
        else: 
            print_warning(req, "The search type '%s' is not defined." % ti, "Error")
            return(1)
        if hit_hints:
            results_for_opft_item.calculate_nbhits()
            if results_for_opft_item._nbhits == 0:                
                hit_hints_displayed = 1
                text = "Search term <strong>%s</strong>" % pi
                if fi:
                    text += " inside <em>%s</em> " % fi
                print_warning(req, "%s did not match any record.  Nearest terms are: %s" %
                              (text, create_nearest_words_links(req.args, pi, fi)), "")                
                
            if dbg:
                print_warning(req, "Item <strong>%s:%s</strong> gave %d hits." %
                              (fi, pi, results_for_opft_item._nbhits), "")                
        # secondly, apply the set intersect/union/disjunction functions:
        if dbg:
            t1 = os.times()[4]
        if results_used:
            if oi == '+':
                results.intersect(results_for_opft_item)
            elif oi == '-':
                results.difference(results_for_opft_item)
            elif oi == '|':
                results.union(results_for_opft_item)
            else:
                print_warning(req, "Invalid set operation %s." % oi, "Error")
        else:
            results = results_for_opft_item
            results_used = 1
        if dbg:
            t2 = os.times()[4]
            print_warning(req, "Set operation '%s' took %.2f seconds." % (oi, (t2 - t1)), "Profile")

    ## return all hits if no collection list specified (useful for WebSearch Admin to determine collection
    ## recID universe):
    if colls == None:        
        results.calculate_nbhits()
        return results

    ## intersect with collection universe:
    if dbg:
        t1 = os.times()[4]
    results_out = {}
    if colls:
        for coll in colls:
            results_out[coll] = HitList()
            results_out[coll]._set = Numeric.bitwise_and(results._set, collrecs_cache[coll]._set)
        if dbg:
            t2 = os.times()[4]
            print_warning(req, "Intersecting with collection hitlist universe took %.2f seconds." % ((t2 - t1)), "Profile")

    ## count number of hits:
    for coll in colls:
        results_out[coll].calculate_nbhits()
        
    if hit_hints and not hit_hints_displayed:
        if results._nbhits == 0: # pattern not found in any public/private collection:
            text = """All search terms matched but boolean query returned no hits.  Please combine your search terms differently."""
            url_args = req.args
            for opft_item in opft_items:
                oi, pi, fi, ti = opft_item[0], opft_item[1], opft_item[2], opft_item[3]
                url_args_new = re.sub(r'(^|\&)p=.*?(\&|$)', r'\1p='+urllib.quote(pi)+r'\2', url_args)
                url_args_new = re.sub(r'(^|\&)f=.*?(\&|$)', r'\1f='+urllib.quote(fi)+r'\2', url_args_new)
                text += """<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<a href="%s/search.py?%s">%s</a> - about %s hits""" % \
                        (weburl, url_args_new, pi, get_word_nbhits(pi, fi))
            print_warning(req, text, "") 
        else: # pattern found but perhaps not in the collections chosen by the user:
            num = 0
            for coll in colls:
                num += results_out[coll]._nbhits
            if num == 0:
                # try to search in Home:
                results_Home = HitList()
                results_Home._set = Numeric.bitwise_and(results._set, collrecs_cache[cdsname]._set)
                results_Home.calculate_nbhits()
                if results_Home._nbhits > 0:
                    # some hits found in Home, so propose this search:
                    url_args = req.args
                    url_args = re.sub(r'(^|\&)cc=.*?(\&|$)', r'\2', url_args)
                    url_args = re.sub(r'(^|\&)c=.*?(\&[^c]+=|$)', r'\2', url_args)
                    url_args = re.sub(r'^\&+', '', url_args)
                    url_args = re.sub(r'\&+$', '', url_args)
                    print_warning(req, """Matches found in other public collections: 
                                  <a href="%s/search.py?%s">%d hits</a>.""" %
                                  (weburl, url_args, results_Home._nbhits), "")
                else:
                    # no hits found in Home, recommend different search terms:
                    text = """All search terms matched but boolean query returned no hits.  Please combine your search terms differently."""
                    url_args = req.args
                    for opft_item in opft_items:
                        oi, pi, fi, ti = opft_item[0], opft_item[1], opft_item[2], opft_item[3]
                        url_args_new = re.sub(r'(^|\&)p=.*?(\&|$)', r'\1p='+urllib.quote(pi)+r'\2', url_args)
                        url_args_new = re.sub(r'(^|\&)f=.*?(\&|$)', r'\1f='+urllib.quote(fi)+r'\2', url_args_new)
                        text += """<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<a href="%s/search.py?%s">%s</a> - about %s hits""" % \
                                (weburl, url_args_new, pi, get_word_nbhits(pi, fi))
                    print_warning(req, text, "")

    return results_out

def search_in_bibwords(req, word, f, decompress=zlib.decompress):
    """Searches for 'word' inside bibwordsX table for field 'f' and returns hitlist of recIDs."""
    set = HitList() # will hold output result set
    set_used = 0 # not-yet-used flag, to be able to circumvent set operations
    # deduce into which bibwordsX table we will search:
    bibwordsX = "bibwords%d" % get_wordsindex_id("anyfield")
    if f:
        wordsindex_id = get_wordsindex_id(f)
        if wordsindex_id:
            bibwordsX = "bibwords%d" % wordsindex_id
        else:
            print_warning(req, "Sorry, the word table for '%s' does not seem to exist.  Choosing the global word file instead." % f, "Error")
    # wash 'word' argument and construct query:
    word = string.replace(word, '*', '%') # we now use '*' as the truncation character
    words = string.split(word, "->", 1) # check for span query
    if len(words) == 2:
        word0 = re_word.sub('', words[0])
        word1 = re_word.sub('', words[1])
        query = "SELECT word,hitlist FROM %s WHERE word BETWEEN '%s' AND '%s'" % (bibwordsX, escape_string(word0[:50]), escape_string(word1[:50]))
    else:
        word = re_word.sub('', word)
        if string.find(word, '%') >= 0: # do we have wildcard in the word?
            query = "SELECT word,hitlist FROM %s WHERE word LIKE '%s'" % (bibwordsX, escape_string(word[:50]))
        else:
            query = "SELECT word,hitlist FROM %s WHERE word='%s'" % (bibwordsX, escape_string(word[:50]))
    if dbg:
        print_warning(req, query, "Debug")
    # launch the query:
    res = run_sql(query)
    # fill the result set:
    for word,hitlist in res:
        if dbg: 
	   t1 = os.times()[4]
        hitlist_bibwrd = HitList(Numeric.loads(decompress(hitlist)))
        if dbg: 
	   t2 = os.times()[4]
	   print_warning(req, "Demarshaling '%s' hitlist took %.2f seconds." % (word, (t2 - t1)), "Profile")
        # add the results:
        if dbg: 
	   t1 = os.times()[4]
        if set_used:
            set.union(hitlist_bibwrd)
        else:            
            set = hitlist_bibwrd
            set_used = 1
        if dbg: 
	   t2 = os.times()[4]
	   print_warning(req, "Adding '%s' hitlist took %.2f seconds." % (word, (t2 - t1)), "Profile")
    # okay, return result set:
    return set

def search_in_bibxxx(req, p, f, type):
    """Searches for pattern 'p' inside bibxxx tables for field 'f' and returns hitlist of recIDs found.
    The search type is defined by 'type' (e.g. equals to 'r' for a regexp search)."""
    p_orig = p # saving for eventual future 'no match' reporting
    # wash arguments:
    f = string.replace(f, '*', '%') # replace truncation char '*' in field definition
    if type == 'r':
        pattern = "REGEXP '%s'" % MySQLdb.escape_string(p)
    else:
        p = string.replace(p, '*', '%') # we now use '*' as the truncation character
        ps = string.split(p, "->", 1) # check for span query:
        if len(ps) == 2:
            pattern = "BETWEEN '%s' AND '%s'" % (MySQLdb.escape_string(ps[0]), MySQLdb.escape_string(ps[1]))
        else:
            pattern = "LIKE '%s'" % MySQLdb.escape_string(ps[0])            
    # construct 'tl' which defines the tag list (MARC tags) to search in:
    tl = []
    if str(f[0]).isdigit() and str(f[1]).isdigit():
        tl.append(f) # 'f' seems to be okay as it starts by two digits
    else:
        # convert old ALEPH tag names, if appropriate: (TODO: get rid of this before entering this function)
        if cfg_fields_convert.has_key(string.lower(f)): 
            f = cfg_fields_convert[string.lower(f)]
        # deduce desired MARC tags on the basis of chosen 'f'
        tl = get_field_tags(f)
        if not tl:
            # by default we are searching in author index:
            tl = get_field_tags("author")
            print_warning(req, "The phrase or access file search does not work in this field.  Choosing author index instead.", "Warning")
    # okay, start search:
    l = [] # will hold list of recID that matched
    for t in tl:
        # deduce into which bibxxx table we will search:
        digit1, digit2 = int(t[0]), int(t[1])
        bx = "bib%d%dx" % (digit1, digit2)
        bibx = "bibrec_bib%d%dx" % (digit1, digit2)
        # construct query:
        if len(t) != 6 or t[-1:]=='%': # only the beginning of field 't' is defined, so add wildcard character:
            query = "SELECT bibx.id_bibrec FROM %s AS bx LEFT JOIN %s AS bibx ON bx.id=bibx.id_bibxxx WHERE bx.value %s AND bx.tag LIKE '%s%%'" %\
                    (bx, bibx, pattern, t)
        else:
            query = "SELECT bibx.id_bibrec FROM %s AS bx LEFT JOIN %s AS bibx ON bx.id=bibx.id_bibxxx WHERE bx.value %s AND bx.tag='%s'" %\
                    (bx, bibx, pattern, t)
        if dbg:
            print_warning(req, query, "Debug")
        # launch the query:
        res = run_sql(query)
        # fill the result set:
        for id_bibrec in res:
            if id_bibrec[0]:
                l.append(id_bibrec[0])
    # check no of hits found:
    nb_hits = len(l)
    if dbg:
        print_warning(req, "The pattern '%s' in field '%s' has got '%d' hits." % (p, f, nb_hits), "Info")
    # check whether it is sound to do a new search:
    if nb_hits == 0 and not (p.startswith("%") and p.endswith("%")):
        # try to launch substring search:
        p_new = "%" + p + "%"
        print_warning(req, "Your original search for <strong>%s</strong> within <strong>%s</strong> field did not return any result.  Looking for subphrase/substring match..." % \
                      (p_orig, f), "Info")
        return search_in_bibxxx(req, p_new, f, type)
    else:
        # okay, return result set:
        set = HitList()
        set.addlist(Numeric.array(l))
        return set

def search_in_bibrec(day1, day2, type='creation_date'):
    """Return hitlist of recIDs found that were either created or modified (see 'type' arg)
       from day1 until day2, inclusive.  Does not pay attention to pattern, collection, anything.
       Useful to intersect later on with the 'real' query."""
    set = HitList()
    if type != "creation_date" and type != "modification_date":
        # type argument is invalid, so search for creation dates by default
        type = "creation_date"
    res = run_sql("SELECT id FROM bibrec WHERE %s>=%s AND %s<=%s" % (type, "%s", type, "%s"),
                  (day1, day2))
    l = []
    for row in res:
        l.append(row[0])        
    set.addlist(Numeric.array(l))
    return set

def create_nearest_words_links(url, p, f, n=10, prologue="<br>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;", epilogue=""):
    """Return list of 'n' nearest words to 'p' in the words index list for the field 'f'."""
    out = ""
    # deduce into which bibwordsX table we will search:
    bibwordsX = "bibwords%d" % get_wordsindex_id("anyfield")
    if f:
        wordsindex_id = get_wordsindex_id(f)
        if wordsindex_id:
            bibwordsX = "bibwords%d" % wordsindex_id
        else:
            return "%sNo words index available for %s.%s" % (prologue, f, epilogue)
    # try to get nearest n words:
    query = "SELECT word FROM %s WHERE word LIKE '%s%%' LIMIT %d" % (bibwordsX, escape_string(p), n)
    res = run_sql(query)
    for row in res:
        p_new = row[0]
        p_new_quoted = urllib.quote(p_new,'')
        url_new = url
        if p:
            p_quoted = urllib.quote(p,'')
            url_new = string.replace(url, p_quoted, p_new_quoted)
        else:
            url_new = url + "&p=%s" % p_new_quoted
        out += """%s<a href="%s/search.py?%s">%s</a> - about %s hits%s""" % \
               (prologue, weburl, url_new, p_new, get_word_nbhits(p_new, f), epilogue)
    if out:
        return out
    else:
        # try search pattern of lesser length:
        if p:
            p_quoted = urllib.quote(p,'')
            p_new_quoted = urllib.quote(p[:-1],'')
            return create_nearest_words_links(string.replace(url, p_quoted, p_new_quoted), p[:-1], f, n)
        else:
            return "%sNo words index available for this query.%s" % (prologue, epilogue)

def get_word_nbhits(word, f):
    """Return number of hits for word 'word' inside words index for field 'f'."""
    out = 0
    # deduce into which bibwordsX table we will search:
    bibwordsX = "bibwords%d" % get_wordsindex_id("anyfield")
    if f:
        wordsindex_id = get_wordsindex_id(f)
        if wordsindex_id:
            bibwordsX = "bibwords%d" % wordsindex_id
        else:
            return 0
    # try to get nearest n words:
    if word:
        query = "SELECT hitlist FROM %s WHERE word LIKE '%s'" % (bibwordsX, escape_string(word))
        res = run_sql(query)
        for hitlist in res:
            out += Numeric.sum(Numeric.loads(zlib.decompress(hitlist[0])).copy().astype(Numeric.Int))
    return nice_number(out)

def get_mysql_recid_from_aleph_sysno(sysno):
    """Returns MySQL's recID for ALEPH sysno passed in the argument (e.g. "2209836CERCER").
       Returns None in case of failure."""
    out = None
    query = "SELECT bb.id_bibrec FROM bibrec_bib90x AS bb, bib90x AS b WHERE b.value='%s' AND b.tag='909C0o' AND bb.id_bibxxx=b.id" %\
            (escape_string(sysno))
    res = run_sql(query, None, 1)
    if res:        
        out = res[0][0]
    return out

def get_field_tags(field):
    """Returns a list of MARC tags for the field code 'field'.
       Returns empty list in case of error.
       Example: field='author', output=['100__%','700__%']."""
    out = []
    query = """SELECT t.value FROM tag AS t, field_tag AS ft, field AS f
                WHERE f.code='%s' AND ft.id_field=f.id AND t.id=ft.id_tag
                ORDER BY ft.score DESC""" % field
    res = run_sql(query)
    for val in res:
        out.append(val[0])
    return out

def get_fieldvalues(recID, tag):
    """Return list of field values for field 'tag' inside record 'recID'."""
    out = []
    if tag == "001___":
        # we have asked for recID that is not stored in bibXXx tables
        out.append(str(recID))
    else:
        # we are going to look inside bibXXx tables
        digit = tag[0:2]
        bx = "bib%sx" % digit
        bibx = "bibrec_bib%sx" % digit
        query = "SELECT bx.value FROM %s AS bx, %s AS bibx WHERE bibx.id_bibrec='%s' AND bx.id=bibx.id_bibxxx AND bx.tag LIKE '%s'" \
                % (bx, bibx, recID, tag)
        res = run_sql(query)
        for row in res:
            out.append(row[0])
    return out

def get_fieldvalues_alephseq_like(recID, tags):
    """Return textual lines in ALEPH sequential like format for field 'tag' inside record 'recID'."""
    out = ""    
    # clean passed 'tag':
    tags_in = string.split(tags, ",")
    if len(tags_in) == 1 and len(tags_in[0]) == 6:
        ## case A: one concrete subfield asked, so print its value if found
        ##         (use with care: can false you if field has multiple occurrences)
        out += string.join(get_fieldvalues(recID, tags_in[0]),"\n")
    else:
        ## case B: print our "text MARC" format; works safely all the time        
        tags_out = []
        for tag in tags_in:
            if len(tag) == 0:
                for i in range(0,10):
                    for j in range(0,10):
                        tags_out.append("%d%d%%" % (i, j))
            elif len(tag) == 1:
                for j in range(0,10):
                    tags_out.append("%s%d%%" % (tag, j))        
            elif len(tag) < 5:
                tags_out.append("%s%%" % tag)
            elif tag >= 6:
                tags_out.append(tag[0:5])
        # search all bibXXx tables as needed:
        for tag in tags_out:
            digits = tag[0:2]
            if tag.startswith("001") or tag.startswith("00%"):
                if out:
                    out += "\n"
                out += "%09d %s %d" % (recID, "001__", recID)
            bx = "bib%sx" % digits
            bibx = "bibrec_bib%sx" % digits
            query = "SELECT b.tag,b.value,bb.field_number FROM %s AS b, %s AS bb "\
                    "WHERE bb.id_bibrec='%s' AND b.id=bb.id_bibxxx AND b.tag LIKE '%s%%' "\
                    "ORDER BY bb.field_number, b.tag ASC" % (bx, bibx, recID, tag)
            res = run_sql(query)
            # go through fields:
            field_number_old = -999
            field_old = ""
            for row in res:
                field, value, field_number = row[0], row[1], row[2]
                ind1, ind2 = field[3], field[4]
                if ind1 == "_":
                    ind1 = ""
                if ind2 == "_":
                    ind2 = ""                        
                # print field tag
                if field_number != field_number_old or field[:-1] != field_old[:-1]:
                    if out:
                        out += "\n"
                    out += "%09d %s " % (recID, field[:5])
                    field_number_old = field_number
                    field_old = field
                # print subfield value
                out += "$$%s%s" % (field[-1:], value)
    return out

def record_exists(recID):
    "Returns 1 if record 'recID' exists.  Returns 0 otherwise."
    out = 0
    query = "SELECT id FROM bibrec WHERE id='%s'" % recID
    res = run_sql(query, None, 1)
    if res:        
        out = 1
    return out    

def get_creation_date(recID):
    "Returns the creation date of the record 'recID'."
    out = ""
    query = "SELECT DATE_FORMAT(creation_date,'%%Y-%%m-%%d') FROM bibrec WHERE id='%s'" % (recID)
    res = run_sql(query, None, 1)
    if res:        
        out = res[0][0]
    return out

def get_modification_date(recID):
    "Returns the date of last modification for the record 'recID'."
    out = ""
    query = "SELECT DATE_FORMAT(modification_date,'%%Y-%%m-%%d') FROM bibrec WHERE id='%s'" % (recID)
    res = run_sql(query, None, 1)
    if res:        
        out = res[0][0]
    return out

def print_warning(req, msg, type='Tip', prologue='', epilogue='<br>'):
    "Prints warning message and flushes output."
    if req:
        req.write('%s<span class="quicknote">' % (prologue))
        if type:
            req.write('<strong>%s:</strong> ' % type)
        req.write('%s</span>%s' % (msg, epilogue))

def print_search_info(p, f, sf, so, sp, of, ot, collection=cdsname, nb_found=-1, jrec=1, rg=10,
                      as=0, p1="", p2="", p3="", f1="", f2="", f3="", m1="", m2="", m3="", op1="", op2="",
                      d1y="", d1m="", d1d="", d2y="", d2m="", d2d="",
                      cpu_time=-1, middle_only=0):
    """Prints stripe with the information on 'collection' and 'nb_found' results and CPU time.
       Also, prints navigation links (beg/next/prev/end) inside the results set.
       If middle_only is set to 1, it will only print the middle box information (beg/netx/prev/end/etc) links.
       This is suitable for displaying navigation links at the bottom of the search results page."""

    out = ""
    # left table cells: print collection name
    if not middle_only:
        out += "\n<a name=\"%s\"></a>" \
              "\n<form action=\"%s/search.py\" method=\"get\">"\
              "\n<table width=\"100%%\" cellpadding=2 cellspacing=0 border=0><tr class=\"results\"><td align=\"left\">" \
              "<strong><big><img align=\"left\" src=\"%s/img/okay.gif\" width=20 height=20 alt=\"*\">"\
              "<a href=\"%s/?c=%s&as=%d\">%s</a></big></strong></td>\n" % \
              (collection, weburl, weburl, weburl, urllib.quote_plus(collection), as, collection)
    else:
        out += """\n<form action="%s/search.py" method="get"><div align="center">\n""" % weburl

    # sanity check:
    if jrec < 1:
        jrec = 1
    if jrec > nb_found:
        jrec = max(nb_found-rg+1, 1)        

    # middle table cell: print beg/next/prev/end arrows:
    if not middle_only:
        out += "<td align=\"center\">\n"
        out += "<strong>%s</strong> records found: &nbsp; \n" % nice_number(nb_found)
    else:
        out += "<small>"
        if nb_found > rg:
            out += "%s: <strong>%s</strong> records found: &nbsp; " % (collection, nice_number(nb_found))

    if nb_found > rg: # navig.arrows are needed, since we have many hits
        url = '%s/search.py?p=%s&amp;c=%s&amp;f=%s&amp;sf=%s&amp;so=%s&amp;sp=%s&amp;of=%s&amp;ot=%s' % (weburl, urllib.quote(p), urllib.quote(collection), f, sf, so, sp, of, ot)
        url += '&amp;as=%s&amp;p1=%s&amp;p2=%s&amp;p3=%s&amp;f1=%s&amp;f2=%s&amp;f3=%s&amp;m1=%s&amp;m2=%s&amp;m3=%s&amp;op1=%s&amp;op2=%s' \
               % (as, urllib.quote(p1), urllib.quote(p2), urllib.quote(p3), f1, f2, f3, m1, m2, m3, op1, op2)
        url += '&amp;d1y=%s&amp;d1m=%s&amp;d1d=%s&amp;d2y=%s&amp;d2m=%s&amp;d2d=%s' \
               % (d1y, d1m, d1d, d2y, d2m, d2d)
        if jrec-rg > 1:
            out += "<a class=\"img\" href=\"%s&amp;jrec=1&amp;rg=%d\"><img src=\"%s/img/sb.gif\" alt=\"begin\" border=0></a>" % (url, rg, weburl)
        if jrec > 1:
            out += "<a class=\"img\" href=\"%s&amp;jrec=%d&amp;rg=%d\"><img src=\"%s/img/sp.gif\" alt=\"previous\" border=0></a>" % (url, max(jrec-rg,1), rg, weburl)
        if nb_found > rg:
            out += "%d - %d" % (jrec, jrec+rg-1)
        else:
            out += "%d - %d" % (jrec, nb_found)
        if nb_found >= jrec+rg:
            out += "<a class=\"img\" href=\"%s&amp;jrec=%d&amp;rg=%d\"><img src=\"%s/img/sn.gif\" alt=\"next\" border=0></a>" % \
                  (url, jrec+rg, rg, weburl)
        if nb_found >= jrec+rg+rg:
            out += "<a class=\"img\" href=\"%s&amp;jrec=%d&amp;rg=%d\"><img src=\"%s/img/se.gif\" alt=\"end\" border=0></a>" % \
                  (url, nb_found-rg+1, rg, weburl)
        out += "<input type=\"hidden\" name=\"p\" value=\"%s\">" % p
        out += "<input type=\"hidden\" name=\"c\" value=\"%s\">" % collection
        out += "<input type=\"hidden\" name=\"f\" value=\"%s\">" % f 
        out += "<input type=\"hidden\" name=\"sf\" value=\"%s\">" % sf
        out += "<input type=\"hidden\" name=\"so\" value=\"%s\">" % so
        out += "<input type=\"hidden\" name=\"of\" value=\"%s\">" % of
        if ot:
            out += """<input type="hidden" name="ot" value="%s">""" % ot
        if sp:
            out += """<input type="hidden" name="sp" value="%s">""" % sp 
        out += "<input type=\"hidden\" name=\"rg\" value=\"%d\">" % rg
        out += "<input type=\"hidden\" name=\"as\" value=\"%d\">" % as
        out += "<input type=\"hidden\" name=\"p1\" value=\"%s\">" % p1
        out += "<input type=\"hidden\" name=\"p2\" value=\"%s\">" % p2
        out += "<input type=\"hidden\" name=\"p3\" value=\"%s\">" % p3
        out += "<input type=\"hidden\" name=\"f1\" value=\"%s\">" % f1
        out += "<input type=\"hidden\" name=\"f2\" value=\"%s\">" % f2
        out += "<input type=\"hidden\" name=\"f3\" value=\"%s\">" % f3
        out += "<input type=\"hidden\" name=\"m1\" value=\"%s\">" % m1
        out += "<input type=\"hidden\" name=\"m2\" value=\"%s\">" % m2
        out += "<input type=\"hidden\" name=\"m3\" value=\"%s\">" % m3
        out += "<input type=\"hidden\" name=\"op1\" value=\"%s\">" % op1
        out += "<input type=\"hidden\" name=\"op2\" value=\"%s\">" % op2
        out += "<input type=\"hidden\" name=\"d1y\" value=\"%s\">" % d1y
        out += "<input type=\"hidden\" name=\"d1m\" value=\"%s\">" % d1m
        out += "<input type=\"hidden\" name=\"d1d\" value=\"%s\">" % d1d
        out += "<input type=\"hidden\" name=\"d2y\" value=\"%s\">" % d2y
        out += "<input type=\"hidden\" name=\"d2m\" value=\"%s\">" % d2m
        out += "<input type=\"hidden\" name=\"d2d\" value=\"%s\">" % d2d
        out += "&nbsp; or jump to record: <input type=\"text\" name=\"jrec\" size=\"4\" value=\"%d\">" % jrec
    if not middle_only:
        out += "</td>"
    else:
        out += "</small>"
        
    # right table cell: cpu time info
    if not middle_only:
        if cpu_time > -1:
            out +="<td align=\"right\"><small>Search took %.2f sec.</small>&nbsp;</td>" % cpu_time
        out += "</tr></table>"
    else:
        out += "</div>"
    out += "</form>"
    return out

def print_results_overview(colls, results_final_nb_total, results_final_nb, cpu_time):
    "Prints results overview box with links to particular collections below."
    out = ""
    if len(colls) == 1:
        # if one collection only, print nothing:
        return out
    # first find total number of hits:
    out += "<p><table width=\"100%%\" border=0 cellspacing=0 cellpadding=2>" \
           "<tr><td class=\"results\"><strong>Results overview:</strong> Found <strong>%s</strong> records in %.2f seconds.</td></tr>" % \
           (nice_number(results_final_nb_total), cpu_time)
    # then print hits per collection:
    out += "<tr><td class=\"resultsbis\">"
    for coll in colls:
        if results_final_nb[coll] > 0:
            out += "In <strong><a href=\"#%s\">%s</a></strong>, " \
                  "<a href=\"#%s\">%s records found</a><br>" \
                  % (urllib.quote(coll), coll, urllib.quote(coll), nice_number(results_final_nb[coll]))
    out += "</td></tr></table>\n"
    return out

def sort_records(req, recIDs, sort_field='', sort_order='d', sort_pattern=''):
    """Sort records in 'recIDs' list according sort field 'sort_field' in order 'sort_order'.
       If more than one instance of 'sort_field' is found for a given record, try to choose that that is given by
       'sort pattern', for example "sort by report number that starts by CERN-PS".
       Note that 'sort_field' can be field code like 'author' or MARC tag like '100__a' directly."""

    ## check arguments:
    if not sort_field:
        return recIDs
    if len(recIDs) > cfg_nb_records_to_sort:
        print_warning(req, "Sorry, sorting is allowed on sets of up to %d records only.  Using default sort order (\"latest first\")." % cfg_nb_records_to_sort,"Warning")
        return recIDs

    recIDs_dict = {}
    recIDs_out = []

    ## first deduce sorting MARC tag out of the 'sort_field' argument:
    tags = []
    if sort_field and str(sort_field[0:2]).isdigit():
        # sort_field starts by two digits, so this is probably a MARC tag already
        tags.append(sort_field)
    else:
        # let us check the 'field' table
        query = """SELECT DISTINCT(t.value) FROM tag AS t, field_tag AS ft, field AS f
                    WHERE f.code='%s' AND ft.id_field=f.id AND t.id=ft.id_tag
                    ORDER BY ft.score DESC""" % sort_field
        res = run_sql(query)
        if res:
            for row in res:
                tags.append(row[0])
        else:
            print_warning(req, "Sorry, '%s' does not seem to be a valid sort option.  Choosing title sort instead." % sort_field, "Error")
            tags.append("245__a")
        
    ## check if we have sorting tag defined:
    if tags:
        # fetch the necessary field values:
        for recID in recIDs:
            val = "" # will hold value for recID according to which sort
            vals = [] # will hold all values found in sorting tag for recID
            for tag in tags:
                vals.extend(get_fieldvalues(recID, tag))
            if sort_pattern: 
                # try to pick that tag value that corresponds to sort pattern
                bingo = 0
                for v in vals: 
                    if v.startswith(sort_pattern): # bingo!
                        bingo = 1
                        val = v
                        break
                if not bingo: # not found, so joint them all together
                    val = string.join(vals)                    
            else:
                # no sort pattern defined, so join them all together
                val = string.join(vals)
            val = val.lower()
            if recIDs_dict.has_key(val):
                recIDs_dict[val].append(recID)
            else:
                recIDs_dict[val] = [recID]
        # sort them:
        recIDs_dict_keys = recIDs_dict.keys()
        recIDs_dict_keys.sort()
        # now that keys are sorted, create output array:
        for k in recIDs_dict_keys:
            for s in recIDs_dict[k]:
                recIDs_out.append(s)        
        # ascending or descending?
        if sort_order == 'a':
            recIDs_out.reverse()
        # okay, we are done
        return recIDs_out
    else:
        # good, no sort needed
        return recIDs
        
def print_records(req, recIDs, jrec=1, rg=10, format='hb', ot='', decompress=zlib.decompress):
    """Prints list of records 'recIDs' formatted accoding to 'format' in groups of 'rg' starting from 'jrec'.
    Assumes that the input list 'recIDs' is sorted in reverse order, so it counts records from tail to head.
    A value of 'rg=-9999' means to print all records: to be used with care.
    """

    # sanity checking:
    if req == None:
        return

    if len(recIDs):
        nb_found = len(recIDs)

        if rg == -9999: # print all records
            rg = nb_found
        else:
            rg = abs(rg)
        if jrec < 1: # sanity checks
            jrec = 1
        if jrec > nb_found:
            jrec = max(nb_found-rg+1, 1)

        # will print records from irec_max to irec_min excluded:
        irec_max = nb_found - jrec
        irec_min = nb_found - jrec - rg
        if irec_min < 0:
            irec_min = -1
        if irec_max >= nb_found:
            irec_max = nb_found - 1
        
        #req.write("%s:%d-%d" % (recIDs, irec_min, irec_max))

        if format.startswith('x'):
            # we are doing XML output:
            for irec in range(irec_max,irec_min,-1):
                req.write(print_record(recIDs[irec], format, ot))

        elif format.startswith('t') or str(format[0:3]).isdigit():
            # we are doing plain text output:
            for irec in range(irec_max,irec_min,-1):
                x = print_record(recIDs[irec], format, ot)
                req.write(x)
                if x:
                    req.write('\n')
        else:
            # we are doing HTML output:
            if format != "hb":
                # deduce url without 'of' argument:
                url_args = re.sub(r'(^|\&)of=.*?(\&|$)',r'\1',req.args)
                url_args = re.sub(r'^\&+', '', url_args)
                url_args = re.sub(r'\&+$', '', url_args)
                # print other formatting choices:
                req.write("""<p><div align="right"><small>Format: \n""")
                if format != "hm":
                    req.write('HTML | <a href="%s/search.py?%s&of=hm">HTML MARC</a> | <a href="%s/search.py?%s&of=xd">XML DC</a> | <a href="%s/search.py?%s&of=xm">XML MARC</a>' % (weburl, url_args, weburl, url_args, weburl, url_args))
                else:
                    req.write('<a href="%s/search.py?%s">HTML</a> | HTML MARC | <a href="%s/search.py?%s&of=xd">XML DC</a> | <a href="%s/search.py?%s&of=xm">XML MARC</a>' % (weburl, url_args, weburl, url_args, weburl, url_args))
                req.write("</small></div>\n")
                for irec in range(irec_max,irec_min,-1):
                    req.write(print_record(recIDs[irec], format, ot))
                    req.write("<p><p>")
            else:                
                req.write("\n<ol start=\"%d\">" % jrec)            
                for irec in range(irec_max,irec_min,-1):
                    req.write("\n<li>")
                    req.write(print_record(recIDs[irec], format, ot))
                    req.write("<p>")
                req.write("\n</ol>")

    else:        
        print_warning(req, 'Use different search terms.')        

def print_record(recID, format='hb', ot='', decompress=zlib.decompress):
    "Prints record 'recID' formatted accoding to 'format'."
    out = ""

    # sanity check:
    if not record_exists(recID):
        return out

    # print record opening tags, if needed:
    if format == "marcxml" or format == "oai_dc":
        out += "  <record>\n"
        out += "   <header>\n"
        for id in get_fieldvalues(recID,oaiidfield):
            out += "    <identifier>%s</identifier>\n" % id
        out += "    <datestamp>%s</datestamp>\n" % get_modification_date(recID)
        out += "   </header>\n"
        out += "   <metadata>\n"

    if format.startswith("xm") or format == "marcxml":
        # look for detailed format existence:
        query = "SELECT value FROM bibfmt WHERE id_bibrec='%s' AND format='%s'" % (recID, format)
        res = run_sql(query, None, 1)
        if res:
            # record 'recID' is formatted in 'format', so print it
            out += "%s" % decompress(res[0][0])
        else:
            # record 'recID' is not formatted in 'format' -- they are not in "bibfmt" table; so fetch all the data from "bibXXx" tables:
            if format == "marcxml":
                out += """    <record xmlns="http://www.loc.gov/MARC21/slim">\n"""
                out += "        <controlfield tag=\"001\">%d</controlfield>\n" % int(recID)
            elif format.startswith("xm"):
                out += """    <record>\n"""
                out += "        <controlfield tag=\"001\">%d</controlfield>\n" % int(recID)
            for digit1 in range(0,10):
                for digit2 in range(0,10):
                    bx = "bib%d%dx" % (digit1, digit2)
                    bibx = "bibrec_bib%d%dx" % (digit1, digit2)
                    query = "SELECT b.tag,b.value,bb.field_number FROM %s AS b, %s AS bb "\
                            "WHERE bb.id_bibrec='%s' AND b.id=bb.id_bibxxx AND b.tag LIKE '%s%%' "\
                            "ORDER BY bb.field_number, b.tag ASC" % (bx, bibx, recID, str(digit1)+str(digit2))
                    if dbg:
                        out += "<br>Debug: " + query
                    res = run_sql(query)
                    field_number_old = -999
                    field_old = ""
                    for row in res:
                        field, value, field_number = row[0], row[1], row[2]
                        ind1, ind2 = field[3], field[4]
                        if ind1 == "_":
                            ind1 = ""
                        if ind2 == "_":
                            ind2 = ""                        
                        # print field tag
                        if field_number != field_number_old or field[:-1] != field_old[:-1]:
                            if format.startswith("xm") or format == "marcxml":

                                fieldid = encode_for_xml(field[0:3])

                                if field_number_old != -999:
                                    out += """        </datafield>\n"""

                                out += """        <datafield tag="%s" ind1="%s" ind2="%s">\n""" % (encode_for_xml(field[0:3]), encode_for_xml(ind1), encode_for_xml(ind2))

                            field_number_old = field_number
                            field_old = field
                        # print subfield value
                        if format.startswith("xm") or format == "marcxml":
                            value = encode_for_xml(value)
                            out += """            <subfield code="%s">%s</subfield>\n""" % (encode_for_xml(field[-1:]), value)

                    # all fields/subfields printed in this run, so close the tag:
                    if (format.startswith("xm") or format == "marcxml") and field_number_old != -999:
                        out += """        </datafield>\n"""
            # we are at the end of printing the record:
            if format.startswith("xm") or format == "marcxml":
                out += "    </record>\n"

    elif format == "xd" or format == "oai_dc":
        # XML Dublin Core format, possibly OAI -- select only some bibXXx fields:
        out += """    <dc xmlns="http://purl.org/dc/elements/1.1/"
                         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                         xsi:schemaLocation="http://purl.org/dc/elements/1.1/
                                             http://www.openarchives.org/OAI/1.1/dc.xsd">\n"""
        for f in get_fieldvalues(recID, "041__a"):
            out += "        <language>%s</language>\n" % f

        for f in get_fieldvalues(recID, "100__a"):
            out += "        <creator>%s</creator>\n" % encode_for_xml(f)

        for f in get_fieldvalues(recID, "700__a"):
            out += "        <creator>%s</creator>\n" % encode_for_xml(f)

        for f in get_fieldvalues(recID, "245__a"):
            out += "        <title>%s</title>\n" % encode_for_xml(f)

        for f in get_fieldvalues(recID, "65017a"):
            out += "        <subject>%s</subject>\n" % encode_for_xml(f)

        for f in get_fieldvalues(recID, "8564_u"):
            out += "        <identifier>%s</identifier>\n" % encode_for_xml(f)
        
        for f in get_fieldvalues(recID, "520__a"):
            out += "        <description>%s</description>\n" % encode_for_xml(f)            

        out += "        <date>%s</date>\n" % get_creation_date(recID)
        out += "    </dc>\n"                    

    elif str(format[0:3]).isdigit():
        # user has asked to print some fields only
        if format == "001":
            out += "<!--%s-begin-->%s<!--%s-end-->\n" % (format, recID, format)
        else:
            vals = get_fieldvalues(recID, format)
            for val in vals:
                out += "<!--%s-begin-->%s<!--%s-end-->\n" % (format, val, format)

    elif format.startswith('t'):
        ## user directly asked for some tags to be displayed only
        out += get_fieldvalues_alephseq_like(recID, ot)

    elif format == "hm":
        out += "<pre>" + get_fieldvalues_alephseq_like(recID, ot) + "</pre>"

    elif format.startswith("h") and ot:
        ## user directly asked for some tags to be displayed only
        out += "<pre>" + get_fieldvalues_alephseq_like(recID, ot) + "</pre>"

    elif format == "hd":
        # HTML detailed format
        # look for detailed format existence:
        query = "SELECT value FROM bibfmt WHERE id_bibrec='%s' AND format='%s'" % (recID, format)
        res = run_sql(query, None, 1)
        if res:
            # record 'recID' is formatted in 'format', so print it
            out += "%s" % decompress(res[0][0])
        else:
            # record 'recID' is not formatted in 'format', so either call BibFormat on the fly or use default format
            # second, see if we are calling BibFormat on the fly:
            if cfg_call_bibformat:
                out += call_bibformat(recID)
            else:
                # okay, need to construct a simple "Detailed record" format of our own:
                out += "<p>&nbsp;"
                # secondly, title:
                titles = get_fieldvalues(recID, "245__a")
                for title in titles:
                    out += "<p><p><center><big><strong>%s</strong></big></center>" % title
                # thirdly, authors:
                authors = get_fieldvalues(recID, "100__a") + get_fieldvalues(recID, "700__a")
                if authors:
                    out += "<p><p><center>"
                    for author in authors:
                        out += """<a href="%s/search.py?p=%s&f=author">%s</a> ;""" % (weburl, urllib.quote(author), author)
                    out += "</center>"
                # fourthly, date of creation:
                dates = get_fieldvalues(recID, "260__c")
                for date in dates:
                    out += "<p><center><small>%s</small></center>" % date
                # fifthly, abstract:
                abstracts = get_fieldvalues(recID, "520__a")
                for abstract in abstracts:
                    out += """<p style="margin-left: 15%%; width: 70%%">
                             <small><strong>Abstract:</strong> %s</small></p>""" % abstract
                # fifthly bis, keywords:
                keywords = get_fieldvalues(recID, "6531_a")
                if len(keywords):
                    out += """<p style="margin-left: 15%; width: 70%">
                             <small><strong>Keyword(s):</strong></small>"""
                    for keyword in keywords:
                        out += """<small><a href="%s/search.py?p=%s&f=keyword">%s</a> ;</small> """ % (weburl, urllib.quote(keyword), keyword)
                # fifthly bis bis, published in:
                prs_p = get_fieldvalues(recID, "909C4p")
                prs_v = get_fieldvalues(recID, "909C4v")
                prs_y = get_fieldvalues(recID, "909C4y")
                prs_n = get_fieldvalues(recID, "909C4n")
                prs_c = get_fieldvalues(recID, "909C4c")
                for idx in range(0,len(prs_p)):
                    out += """<p style="margin-left: 15%%; width: 70%%">
                             <small><strong>Publ. in:</strong> %s"""  % prs_p[idx]
                    if prs_v and prs_v[idx]:
                        out += """<strong>%s</strong>""" % prs_v[idx]
                    if prs_y and prs_y[idx]:
                        out += """(%s)""" % prs_y[idx]
                    if prs_n and prs_n[idx]:
                        out += """, no.%s""" % prs_n[idx]
                    if prs_c and prs_c[idx]:
                        out += """, p.%s""" % prs_c[idx]
                    out += """.</small>"""
                # sixthly, fulltext link:
                urls_z = get_fieldvalues(recID, "8564_z")
                urls_u = get_fieldvalues(recID, "8564_u")
                for idx in range(0,len(urls_u)):
                    link_text = "URL"
                    if urls_z[idx]:
                        link_text = urls_z[idx]
                    out += """<p style="margin-left: 15%%; width: 70%%">
                    <small><strong>%s:</strong> <a href="%s">%s</a></small>""" % (link_text, urls_u[idx], urls_u[idx])
                # print some white space at the end:
                out += "<p><p>"

    else:
        # HTML brief format by default
        query = "SELECT value FROM bibfmt WHERE id_bibrec='%s' AND format='%s'" % (recID, format)
        res = run_sql(query)
        if res:
            # record 'recID' is formatted in 'format', so print it
            out += "%s" % decompress(res[0][0])
        else:
            # record 'recID' does not exist in format 'format', so print some default format:
            # firstly, title:
            titles = get_fieldvalues(recID, "245__a")
            for title in titles:
                out += "<strong>%s</strong> " % title
            # secondly, authors:
            authors = get_fieldvalues(recID, "100__a") + get_fieldvalues(recID, "700__a")
            if authors:
                out += " / "
                for i in range (0,cfg_author_et_al_threshold):
                    if i < len(authors):
                        out += """<a href="%s/search.py?p=%s&f=author">%s</a> ;""" % (weburl, urllib.quote(authors[i]), authors[i])
                if len(authors) > cfg_author_et_al_threshold:
                    out += " <em>et al.</em>"
            # thirdly, date of creation:
            dates = get_fieldvalues(recID, "260__c")
            for date in dates:
                out += " %s." % date
            # thirdly bis, report numbers:
            rns = get_fieldvalues(recID, "037__a")
            for rn in rns:
                out += """ <small class="quicknote">[%s]</small>""" % rn
            rns = get_fieldvalues(recID, "088__a")
            for rn in rns:
                out += """ <small class="quicknote">[%s]</small>""" % rn
            # fourthly, beginning of abstract:
            abstracts = get_fieldvalues(recID, "520__a")
            for abstract in abstracts:
                out += "<br><small>%s [...]</small>" % abstract[:1+string.find(abstract, '.')]
            # fifthly, fulltext link:
            urls_z = get_fieldvalues(recID, "8564_z")
            urls_u = get_fieldvalues(recID, "8564_u")
            for idx in range(0,len(urls_u)):
                out += """<br><small class="note"><a class="note" href="%s">%s</a></small>""" % (urls_u[idx], urls_u[idx])

        # at the end of HTML mode, print "Detailed record" and "Mark record" functions:
        if cfg_use_aleph_sysnos:
            alephsysnos = get_fieldvalues(recID, "909C0o")
            if len(alephsysnos)>0:
                alephsysno = alephsysnos[0]
                out += """<br><span class="moreinfo"><a class="moreinfo" href="%s/search.py?sysnb=%s">Detailed record</a></span>""" \
                       % (weburl, alephsysno)
                #out += """<span class="moreinfo"> - <input name="mark[]" type="checkbox" value="%s"> Mark record</span>""" % alephsysno
            else:
                out += """<br><span class="moreinfo"><a class="moreinfo" href="%s/search.py?id=%s">Detailed record</a></span>""" \
                       % (weburl, recID)
                #out += """<span class="moreinfo"> - <input name="mark[]" type="checkbox" value="%s"> Mark record</span>""" % recID
        else:
            out += """<br><span class="moreinfo"><a class="moreinfo" href="%s/search.py?id=%s">Detailed record</a></span>""" \
                   % (weburl, recID)
            #out += """<span class="moreinfo"> - <input name="mark[]" type="checkbox" value="%s"> Mark record</span>""" % recID

    # print record closing tags, if needed:
    if format == "marcxml" or format == "oai_dc":
        out += "   </metadata>\n"
        out += "  </record>\n"

    return out

def encode_for_xml(s):
    "Encode special chars in string so that it would be XML-compliant."
    s = string.replace(s, '&', '&amp;')
    s = string.replace(s, '<', '&lt;')
    s = unicode(s,'latin-1','ignore').encode('utf-8','replace')
    return s

def call_bibformat(id, otype="HD"):
    """Calls BibFormat for the record 'id'.  Desired BibFormat output type is passed in 'otype' argument.
       This function is mainly used to display full format, if they are not stored in the 'bibfmt' table."""
    f = urllib.urlopen("%s/bibformat/bibformat.shtml?id=%s&otype=%s" % (weburl, id, otype))
    out = f.read()
    f.close()
    return out

def log_query(hostname, query_args, uid=-1):
    """Log query into the query and user_query tables."""
    if uid > 0:
        # log the query only if uid is reasonable
        res = run_sql("SELECT id FROM query WHERE urlargs=%s", (query_args,), 1)
        try:
            id_query = res[0][0]
        except:
            id_query = run_sql("INSERT INTO query (type, urlargs) VALUES ('r', %s)", (query_args,))        
        if id_query:
            run_sql("INSERT INTO user_query (id_user, id_query, hostname, date) VALUES (%s, %s, %s, %s)",
                    (uid, id_query, hostname,
                     time.strftime("%04Y-%02m-%02d %02H%:02M:%02S", time.localtime())))
    return

def log_query_info(action, p, f, colls, nb_records_found_total=-1):
    """Write some info to the log file for later analysis."""
    try:
        log = open(logdir + "/search.log", "a")
        log.write(time.strftime("%04Y%02m%02d%02H%02M%02S#", time.localtime()))
        log.write(action+"#")
        log.write(p+"#")
        log.write(f+"#")
        for coll in colls[:-1]:
            log.write("%s," % coll)
        log.write("%s#" % colls[-1])
        log.write("%d" % nb_records_found_total)
        log.write("\n")
        log.close()
    except:
        pass
    return

### CALLABLES

def perform_request_search(req=None, cc=cdsname, c=None, p="", f="", rg="10", sf="", so="d", sp="", of="hb", ot="", as="0",
                           p1="", f1="", m1="", op1="", p2="", f2="", m2="", op2="", p3="", f3="", m3="", sc="0", jrec="0",
                           id="-1", idb="-1", sysnb="", search="SEARCH",
                           d1y="", d1m="", d1d="", d2y="", d2m="", d2d=""):
    """Perform search, without checking for authentication.  Return list of recIDs found, if of=id.  Otherwise create web page."""    
    # wash passed numerical arguments:
    try:
        id = string.atoi(id)
        idb = string.atoi(idb)
    except:
        id = cfg_max_recID + 1
        idb = cfg_max_recID + 1
    sc = string.atoi(sc)
    jrec = string.atoi(jrec)
    rg = string.atoi(rg)
    as = string.atoi(as)
    # wash dates:
    day1, day2 = wash_dates(d1y, d1m, d1d, d2y, d2m, d2d)    
    if type(of) is list:
        of = of[0]
    if type(ot) is list:
        ot = string.join(ot,",")
    if of.startswith('x'):
        # we are doing XML output:
        req.content_type = "text/xml"
        req.send_http_header()
        req.write("""<?xml version="1.0" encoding="UTF-8"?>\n""")
        if of.startswith("xm"):
            req.write("""<collection xmlns="http://www.loc.gov/MARC21/slim">\n""")
        else:
            req.write("""<collection>\n""")
    elif of.startswith('t') or str(of[0:3]).isdigit():
        # we are doing plain text output:
        req.content_type = "text/plain"
        req.send_http_header()
    elif of == "id":
        # we are passing list of recIDs
        pass
    else:
        # we are doing HTML output:
        req.content_type = "text/html"
        req.send_http_header()
        # detect user:
        try:
            uid = getUid(req)
            req.write(create_header(cc, as, create_user_infobox(uid)))
        except: # ignore user personalisation, needed e.g. for command-line
            req.write(create_header(cc, as))
    if sysnb or id>0:
        ## 1 - detailed record display
        if sysnb: # ALEPH sysnb is passed, so deduce MySQL id for the record:            
            id = get_mysql_recid_from_aleph_sysno(sysnb)
        if of=="hb":
            of = "hd"
        if record_exists(id):
            if idb<=id: # sanity check
                idb=id+1
            print_records(req, range(id,idb), -1, -9999, of, ot)
        else: # record does not exist
            if of.startswith("h"):
                (cc, colls_to_display, colls_to_search) = wash_colls(cc, c, sc)
                p = wash_pattern(p)
                f = wash_field(f)
                req.write(create_search_box(cc, colls_to_display, p, f, rg, sf, so, sp, of, ot, as, p1, f1, m1, op1,
                                            p2, f2, m2, op2, p3, f3, m3, sc, d1y, d1m, d1d, d2y, d2m, d2d))
                print_warning(req, "Requested record does not seem to exist.", None, "<p>")
    elif search == "Browse":
        ## 2 - browse needed
        (cc, colls_to_display, colls_to_search) = wash_colls(cc, c, sc)
        p = wash_pattern(p)
        f = wash_field(f)
        # write search box:
        if of.startswith("h"):
            req.write(create_search_box(cc, colls_to_display, p, f, rg, sf, so, sp, of, ot, as, p1, f1, m1, op1,
                                        p2, f2, m2, op2, p3, f3, m3, sc, d1y, d1m, d1d, d2y, d2m, d2d))
        url = string.replace(req.args, "search=Browse","search=SEARCH")
        if as==1 or (p1 or p2 or p3):
            if p1:
                req.write("<p>Words nearest to <strong>%s</strong> inside <strong>%s</strong> are:<br>" % (p1, f1))
                req.write(create_nearest_words_links(url, p1, f1))
            if p2:
                req.write("<p>Words nearest to <strong>%s</strong> inside <strong>%s</strong> are:<br>" % (p2, f2))
                req.write(create_nearest_words_links(url, p2, f2))
            if p3:
                req.write("<p>Words nearest to <strong>%s</strong> inside <strong>%s</strong> are:<br>" % (p3, f3))
                req.write(create_nearest_words_links(url, p3, f3))
        else:
            req.write("<p>Words nearest to <strong>%s</strong> inside <strong>%s</strong> are:<br>" % (p, f))
            #req.write(create_nearest_words_links(url, p, f))
            browse_pattern(req, colls_to_search, p, f, rg)
    else:
        ## 3 - search needed
        # wash passed collection arguments:
        (cc, colls_to_display, colls_to_search) = wash_colls(cc, c, sc)
        p = wash_pattern(p)
        f = wash_field(f)
        # write search box:
        if of.startswith("h"):
            req.write(create_search_box(cc, colls_to_display, p, f, rg, sf, so, sp, of, ot, as, p1, f1, m1, op1,
                                        p2, f2, m2, op2, p3, f3, m3, sc, d1y, d1m, d1d, d2y, d2m, d2d))
        # run search:
        t1 = os.times()[4]
        if as == 1 or (p1 or p2 or p3):
            # 3A - advanced search
            results_final = search_pattern(req, "", "", colls_to_search)
            if p1:
                results_tmp = search_pattern(req, p1, f1, colls_to_search, m1)
                for coll in colls_to_search: # join results for first advanced search boxen
                    results_final[coll].intersect(results_tmp[coll])
            if p2:
                results_tmp = search_pattern(req, p2, f2, colls_to_search, m2)
                for coll in colls_to_search: # join results for first and second advanced search boxen
                    if op1 == "a": # add
                        results_final[coll].intersect(results_tmp[coll])
                    elif op1 == "o": # or
                        results_final[coll].union(results_tmp[coll])
                    elif op1 == "n": # not
                        results_final[coll].difference(results_tmp[coll])
                    else:
                        print_warning(req, "Invalid set operation %s." % op1, "Error")
            if p3:
                results_tmp = search_pattern(req, p3, f3, colls_to_search, m3)
                for coll in colls_to_search: # join results for second and third advanced search boxen
                    if op2 == "a": # add
                        results_final[coll].intersect(results_tmp[coll])
                    elif op2 == "o": # or
                        results_final[coll].union(results_tmp[coll])
                    elif op2 == "n": # not
                        results_final[coll].difference(results_tmp[coll])
                    else:
                        print_warning(req, "Invalid set operation %s." % op1, "Error")            
            for coll in colls_to_search:
                results_final[coll].calculate_nbhits()
        else:
            # 3B - simple search
            search_cache_key = p+"@"+f+"@"+string.join(colls_to_search,",")
            if search_cache.has_key(search_cache_key): # is the result in search cache?
                results_final = search_cache[search_cache_key]        
            else:       
                results_final = search_pattern(req, p, f, colls_to_search)
                search_cache[search_cache_key] = results_final
            if len(search_cache) > cfg_search_cache_size: # is the cache full? (sanity cleaning)
                search_cache.clear()
        # search done; was there a time restriction?  if yes, apply it now:
        if day1 != "":
            results_of_time_restriction = search_in_bibrec(day1, day2)
            for coll in colls_to_search:
                results_final[coll].intersect(results_of_time_restriction)
                results_final[coll].calculate_nbhits()
        t2 = os.times()[4]
        cpu_time = t2 - t1
        # find total number of records found in each collection
        results_final_nb_total = 0
        results_final_nb = {}
        for coll in colls_to_search:
            results_final_nb[coll] = results_final[coll]._nbhits
            results_final_nb_total += results_final_nb[coll]
        # was there at least one hit?
        if results_final_nb_total == 0:
            # nope, so try similar queries:
            if of.startswith('h'):
                print_warning(req, "No match found.  Trying similar queries...", "", "<p>","<p>")
                req.write("<p>")
                if as==1 or (p1 or p2 or p3):
                    if p1:
                        search_pattern(req, p1, f1, colls_to_search, m1, 1)
                    if p2:
                        search_pattern(req, p2, f2, colls_to_search, m2, 1)
                    if p3:
                        search_pattern(req, p3, f3, colls_to_search, m3, 1)
                else:
                    search_pattern(req, p, f, colls_to_search, None, 1)
        else:
            # yes, some hits found, so print results overview:
            if of == "id":
                # we have been asked to return list of recIDs
                results_final_for_all_colls = HitList()
                for coll in colls_to_search:
                    results_final_for_all_colls.union(results_final[coll])
                return results_final_for_all_colls.items()
            elif of.startswith("h"):
                req.write(print_results_overview(colls_to_search, results_final_nb_total, results_final_nb, cpu_time))
            # print records:
            if len(colls_to_search)>1:
                cpu_time = -1 # we do not want to have search time printed on each collection
            for coll in colls_to_search:
                if results_final[coll]._nbhits:
                    if of.startswith("h"):
                        req.write(print_search_info(p, f, sf, so, sp, of, ot, coll, results_final_nb[coll], jrec, rg, as, p1, p2, p3, f1, f2, f3, m1, m2, m3, op1, op2, d1y, d1m, d1d, d2y, d2m, d2d, cpu_time))
                    results_final_sorted = results_final[coll].items()
                    if sf:
                        results_final_sorted = sort_records(req, results_final_sorted, sf, so, sp)
                    print_records(req, results_final_sorted, jrec, rg, of, ot)
                    if of.startswith("h"):
                        req.write(print_search_info(p, f, sf, so, sp, of, ot, coll, results_final_nb[coll], jrec, rg, as, p1, p2, p3, f1, f2, f3, m1, m2, m3, op1, op2, d1y, d1m, d1d, d2y, d2m, d2d, cpu_time, 1))
            # log query:
            try:
                log_query(req.get_remote_host(), req.args, uid)
            except:
                # do not log query if req is None (used by CLI interface)
                pass
            log_query_info("ss", p, f, colls_to_search, results_final_nb_total)
    # 4 -- write footer:
    if of.startswith('h'):
        req.write(create_footer())
    elif of.startswith('x'):
        req.write("""</collection>\n""")
    # 5 - return value
    if of == "id":
        return []
    else:
        return "\n"

def perform_request_cache(req, action="show"):
    """Manipulates the search engine cache."""
    global search_cache
    global collrecs_cache
    req.content_type = "text/html"
    req.send_http_header() 
    out = ""
    out += "<h1>Search Cache</h1>"
    # clear cache if requested:
    if action == "clear":
        search_cache = {}
        collrecs_cache = create_collrecs_cache()
        collrecs_cache[cdsname] = get_collection_hitlist(cdsname)
    # show collection cache:
    out += "<h3>Collection Cache</h3>"
    out += "<blockquote>"
    for coll in collrecs_cache.keys():
        if collrecs_cache[coll]:
            out += "%s<br>" % coll
    out += "</blockquote>"
    # show search cache:
    out += "<h3>Search Cache</h3>"
    out += "<blockquote>"
    if len(search_cache):
        out += """<table border="=">"""
        out += "<tr><td><strong>%s</strong></td><td><strong>%s</strong></td><td><strong>%s</strong></td><td><strong>%s</strong></td></tr>" % ("Pattern","Field","Collection","Number of Hits")
        for search_cache_key in search_cache.keys():
            p, f, c = string.split(search_cache_key, "@", 2)
            # find out about length of cached data:
            l = 0
            for coll in search_cache[search_cache_key]:
                l += search_cache[search_cache_key][coll]._nbhits
            out += "<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td></tr>" % (p, f, c, l)
        out += "</table>"
    else:
        out += "<p>Search cache is empty."
    out += "</blockquote>"
    out += """<p><a href="%s/search.py/cache?action=clear">clear cache</a>""" % weburl
    req.write(out)
    return "\n"

def perform_request_log(req, date=""):
    """Display search log information for given date."""
    req.content_type = "text/html"
    req.send_http_header() 
    req.write("<h1>Search Log</h1>")
    if date: # case A: display stats for a day
        yyyymmdd = string.atoi(date)
        req.write("<p><big><strong>Date: %d</strong></big><p>" % yyyymmdd)
        req.write("""<table border="1">""")
        req.write("<tr><td><strong>%s</strong></td><td><strong>%s</strong></td><td><strong>%s</strong></td><td><strong>%s</strong></td><td><strong>%s</strong></td><td><strong>%s</strong></td></tr>" % ("No.","Time", "Pattern","Field","Collection","Number of Hits"))
        # read file:
        p = os.popen("grep ^%d %s/search.log" % (yyyymmdd,logdir), 'r')
        lines = p.readlines()
        p.close()
        # process lines:
        i = 0
        for line in lines:
            try:
                datetime, as, p, f, c, nbhits = string.split(line,"#")
                i += 1
                req.write("<tr><td align=\"right\">#%d</td><td>%s:%s:%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" \
                          % (i, datetime[8:10], datetime[10:12], datetime[12:], p, f, c, nbhits))
            except:
                pass # ignore eventual wrong log lines
        req.write("</table>")
    else: # case B: display summary stats per day
        yyyymm01 = int(time.strftime("%04Y%02m01", time.localtime()))
        yyyymmdd = int(time.strftime("%04Y%02m%02d", time.localtime()))
        req.write("""<table border="1">""")
        req.write("<tr><td><strong>%s</strong></td><td><strong>%s</strong></tr>" % ("Day", "Number of Queries"))
        for day in range(yyyymm01,yyyymmdd+1):
            p = os.popen("grep -c ^%d %s/search.log" % (day,logdir), 'r')
            for line in p.readlines():
                req.write("""<tr><td>%s</td><td align="right"><a href="%s/search.py/log?date=%d">%s</a></td></tr>""" % (day, weburl,day,line))
            p.close()
        req.write("</table>")
    return "\n"    

## test cases:
#print collrecs_cache
#collrecs_cache["Preprints"] = get_collection_hitlist("Preprints")
#print perform_search(None, "of","title",["Preprints"])
#print wash_colls(cdsname,"Library Catalogue", 0)
#print wash_colls("Periodicals & Progress Reports",["Periodicals","Progress Reports"], 0)
#print wash_field("wau")
#print print_record(20,"tm","001,245")
#print create_opft_search_units(None, "PHE-87-13","reportnumber")
#print get_word_nbhits("of","title")
#print ":"+wash_pattern("* and % doo * %")+":\n"
#print ":"+wash_pattern("*")+":\n"
#print ":"+wash_pattern("ellis* ell* e*%")+":\n"
#print run_sql("SELECT name,dbquery from collection")
#print get_wordsindex_id("author")
#print get_coll_ancestors("Theses")
#print get_coll_sons("Articles & Preprints")
#print get_coll_real_descendants("Articles & Preprints")
#print get_collection_hitlist("Theses")
#print log(sys.stdin)
#print search_in_bibrec('2002-12-01','2002-12-12')
#print wash_dates('1980', '', '28', '2003','02','')
</protect>
