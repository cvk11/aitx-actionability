#!/usr/bin/env python
"""Illustrative Argon token scenarios, not measured inference predictions. No API."""
from _common import ROOT  # noqa: F401
from aitx.llm.gemini_client import argon_prices

def main():
    pin,pout,_=argon_prices()
    print(f'gemini-4-argon USD/Mtokens: input={pin}, output INCLUDING thinking={pout}')
    print('No cache discount assumed. Input totals include repeated conversation history.')
    for name,inp,out in [('illustrative smaller workload',1_000_000,150_000),('illustrative larger workload',3_000_000,500_000)]:
        cost=(inp*pin+out*pout)/1e6
        print(f'{name}: input={inp}, output+thinking={out}; one=${cost:.2f}, four=${4*cost:.2f}, one with 20% extra billed tokens=${1.2*cost:.2f}')
    print('These are NOT upper bounds or measured ITPR1 costs. A $12 application guard can stop a case before completion.')
    return 0

if __name__=='__main__': raise SystemExit(main())
