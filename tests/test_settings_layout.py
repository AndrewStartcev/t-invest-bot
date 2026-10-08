from html.parser import HTMLParser
from pathlib import Path
import unittest

class Layout(HTMLParser):
    def __init__(self):
        super().__init__(); self.stack=[]; self.ids={}; self.duplicates=[]
    def handle_starttag(self, tag, pairs):
        attrs=dict(pairs)
        if attrs.get('id'):
            key=attrs['id']
            if key in self.ids:self.duplicates.append(key)
            self.ids[key]=(tag,attrs,list(self.stack))
        if tag not in {'input','meta','link','img','hr','br','path'}:self.stack.append((tag,attrs))
    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:self.stack=self.stack[:i];break

class SettingsLayoutTests(unittest.TestCase):
    def test_overview_details_are_tabbed_and_telegram_is_in_settings(self):
        parsed=Layout();parsed.feed((Path(__file__).resolve().parents[1]/'demo/index.html').read_text())
        expected={"today-list":"overview-trades", "overview-portfolio-rows":"overview-portfolio",
                  "instrument-list":"overview-instruments", "demo-positions":"overview-results",
                  "refresh-source":"overview-connection", "telegram-state":"settings-telegram",
                  "telegram-test":"settings-telegram"}
        for field,tab in expected.items():
            with self.subTest(field=field):
                self.assertIn(tab,[attrs.get('id') for _,attrs in parsed.ids[field][2]])
        self.assertNotIn('hidden',parsed.ids['overview-trades'][1])
        for tab in ['portfolio','instruments','results','connection']:
            self.assertIn('hidden',parsed.ids['overview-'+tab][1])
        self.assertEqual(parsed.duplicates,[])

    def test_fields_have_their_tabs_and_original_form_owner(self):
        parsed=Layout();parsed.feed((Path(__file__).resolve().parents[1]/'demo/index.html').read_text())
        self.assertEqual(parsed.duplicates,[])
        groups={'general':['profile','interval','monitor-enabled','auto-buy','real-mode','schedule-start','schedule-end'],
                'policy':['policy-enabled','policy-profit','policy-yield-unit','policy-threshold'],
                'telegram':['token','chat','paused','policy-telegram'],
                'api':['broker-token','broker-account','trade-token','trade-account']}
        groups['limits']=[f'{asset}-{suffix}' for asset in ['stock','bond','fund'] for suffix in ['budget','position','sell-mode','sell-percent','sell-budget']]
        for tab,fields in groups.items():
            for field in fields:
                with self.subTest(field=field):
                    parents=parsed.ids[field][2];ids=[attrs.get('id') for _,attrs in parents]
                    self.assertIn('settings-'+tab,ids)
                    self.assertIn('settings-form',ids)
        password_parents=parsed.ids['current-password'][2]
        self.assertEqual([attrs.get('id') for tag,attrs in password_parents if tag=='form'],['password-form'])
        self.assertIn('novalidate',parsed.ids['settings-form'][1])

if __name__=='__main__':unittest.main()
