#!/usr/bin/env python
#
import tornado.httpserver
import tornado.auth
import tornado.ioloop
import tornado.web
import os
import re
import time
import calendar
import base64
import traceback
import logging
import urllib
import cStringIO
import json
import cgi
import config
import datetime
import crypto
import model
import urlparse
import urllib

class WebHandler(tornado.web.RequestHandler):
  def get_current_user(self):
    return self.get_secure_cookie("uid")

  def get_error_html(self, status_code, **kwargs):
    return "<html><title>Error!</title><style>.box {margin:16px;padding:8px;border:1px solid black;font:14pt Helvetica,arial} "\
            ".small {text-align:right;color:#888;font:italic 8pt Helvetica;}</style>" \
           "<body><div class='box'>We're sorry, something went wrong!<br><br>Perhaps "\
           "you should <a href='/'>return to the front page.</a><br><br><div class='small'>%s %s</div></div>" % (
          status_code, kwargs['exception'])

  def render_platform(self, file, **kwargs):
    target_file = file

    if  "User-Agent" in self.request.headers:
      UA = self.request.headers["User-Agent"]
      if UA.find("iPhone") >= 0:
        target_file = target_file + "_iphone"
    if self.get_argument("cloak", None):
      target_file = file + "_" + self.get_argument("cloak", None)
      
    self.render(target_file + ".html", **kwargs)
    
            
               
class MainHandler(WebHandler):
  def get(self):
    self.set_header("X-XRDS-Location", "https://appstore.mozillalabs.com/xrds")
    uid = self.get_current_user()
    if uid:
      account = model.user(uid)
      if account:
        try:
          account.displayName = account.identities[0].displayName
        except:
          account.displayName = "anonymous"

    else:
      account = None
    
    self.render_platform("index", errorMessage=None, account=account)

class AppHandler(WebHandler):
  def get(self, appID):
    uid = self.get_current_user()
    account = None
    if uid:
      account = model.user(uid)
      if account:
        try:
          account.displayName = account.identities[0].displayName
        except:
          account.displayName = "anonymous"
        
    try:
      theApp = model.application(appID) 
    except:
      return self.redirect("/")
    
    mode = self.get_argument("m", None)
    already_purchased = (model.purchase_for_user_app(uid, appID) != None) # potentially could use purchase metadata?
    
    if (self.request.headers["User-Agent"].find("iPhone") >= 0):
      self.render("app_iphone.html", authorizationURL = "https://appstore.mozillalabs.com/iphone_verify/%d" % int(appID), appID=appID, app=theApp, account=account, mode=mode, alreadyPurchased=already_purchased)
    else:
      self.render("app.html", authorizationURL = "https://appstore.mozillalabs.com/verify/%d" % int(appID), appID=appID, app=theApp, account=account, mode=mode, alreadyPurchased=already_purchased)

class AccountHandler(WebHandler):
  @tornado.web.authenticated
  def get(self):
    uid = self.get_current_user()
    account = model.account(uid)
    self.render("account.html", account=account, error=None)

class LoginHandler(WebHandler):
  def get(self):
    uid = self.get_current_user()
    appID = self.get_argument("app", None)
    return_to = self.get_argument("return_to", None)

    if uid: # that shouldn't happen
      if return_to:
        self.redirect(return_to)
      else:
        self.redirect("/")
    else:
      app = None
      if appID:
        app = model.application(appID)
      
      if not return_to:
        return_to = "/"
      self.render("login.html", app=app, return_to=return_to, error=None)

class LogoutHandler(WebHandler):
  def get(self):
    self.set_cookie("uid", "", expires=datetime.datetime(1970,1,1,0,0,0,0))

    return_to = self.get_argument("return_to", None)
    if return_to:
      self.redirect(return_to)
    else:
      self.redirect("/")

class BuyHandler(WebHandler):
  @tornado.web.authenticated
  def post(self):
    uid = self.get_current_user()
    appid = self.get_argument("appid")
    
    if model.purchase_for_user_app(uid, appid):
      self.write("""{"status":"ok", "message":"User has already purchased that application."}""")
      return 
    else:
      model.createPurchaseForUserApp(uid, appid)
      self.write("""{"status":"ok", "message":"Purchase successful."}""")
      return       

class UnregisterHandler(WebHandler):
  @tornado.web.authenticated
  def get(self, appID):
    uid = self.get_current_user();
    model.remove_purchase_for_user_app(uid, appID)
    self.redirect("/app/%s" % appID)

class VerifyHandler(WebHandler):
  def get(self, appID):
    uid = self.get_current_user()
    try:
      app = model.application(appID)
    except:
      raise ValueError("Unable to load application")
        
    isIPhone = "User-Agent" in self.request.headers and self.request.headers["User-Agent"].find("iPhone") >= 0
        
    # TODO refactor this logic; we're landing here on free apps for iPhone installs
    if app.price == 0 and isIPhone:
      self.render("iphone_verify.html", validationURL=app.launchURL, appName=app.name, appIcon=app.icon96URL, appLaunchScreen=app.icon96URL)
      return

    if app.price != 0 and not uid:
      # user needs to be authenticated to verify a non-free app!
      self.redirect("/login?" + urllib.urlencode({"return_to":"/verify/%s" % appID}))
      return

    if model.purchase_for_user_app(uid, appID):
      
      # Create verification token and sign
      timestamp = datetime.datetime.now()
      verificationToken = "%s|%s|%sT%s" % (uid, appID, timestamp.date(), timestamp.time())
      signature = crypto.sign_verification_token(verificationToken)      

      verifyURL = "%s?%s" % (app.launchURL, urllib.urlencode( { 
        "moz_store.status":"ok",
        "verification":verificationToken,
        "signature":base64.b64encode(signature) } ))

      if isIPhone:
        self.render("iphone_verify.html", validationURL=verifyURL, appIcon=app.icon96URL, appLaunchScreen=app.icon96URL)
      else:
        self.redirect(verifyURL)
        
       
    else:
      # Could potentially provide multiple status codes, e.g. expired
      
      self.redirect("%s?%s" % (app.launchURL, urllib.urlencode( { 
        "moz_store.status":"fail" }) ))

class FederatedLoginHandler(WebHandler):
  def _on_auth(self, user):
    if not user:
      # hm, in the twitter case should we throw?
      self.authenticate_redirect()
      return

    # Couple cases here:
    # 
    # The user has no cookie
    #   Nobody has signed up for this ID yet: create a user, associate this ID with it, cookie the useragent
    #   Somebody has this ID: the user of this ID is the user; cookie the useragent
    # The user has a cookie
    #   Nobody has signed up for this ID yet: associate this ID with the user
    #   This user has this ID: welcome back, just keep going
    #   Somebody ELSE has this ID: we're on a stale session.  we can either switch sessions or report a problem.

    logging.error("In onAuth handler: user is %s" % user)

    identifier = self.getIdentifier(user)
    name = user["name"] if "name" in user else identifier
    email = user["email"] if "email" in user else None
    
    uid = self.get_secure_cookie("uid")
    if not uid:
      ident = model.identity_by_identifier(identifier)
      if ident:
        # welcome back
        self.set_secure_cookie("uid", str(ident.user_id))
      else:
        u = model.createUser()
        uid = u.id
        self.set_secure_cookie("uid", str(uid))
        i = model.addIdentity(uid, identifier, name, email)
    else:
      ident = model.identity_by_identifier(identifier)
      if ident:
        if int(ident.user_id) != int(uid):
          # hm, somebody else has this ID.  the user just switched accounts.
          # this has potential to be confusing, but for now we will switch accounts.
          self.set_secure_cookie("uid", str(ident.user_id))
        else:
          # hm, you've already claimed this identity.  but welcome back anyway.
          pass
        
      else: # add this ident to the user
        i = model.addIdentity(uid, identifier, name, email)
    
    return_to = self.get_argument("to")
    if return_to:
      self.redirect(return_to)
    else:
      self.redirect("/account") # where to?


class OpenIDLoginHandler(FederatedLoginHandler):
  @tornado.web.asynchronous
  def handle_get(self):
    if self.get_argument("openid.mode", None):
      self.get_authenticated_user(self.async_callback(self._on_auth))
      return

    # xheaders doesn't do all the right things to recover
    # from being reverse-proxied: change it up here.

    HACKING = False
    if not HACKING:
      self.request.protocol = "https"
      self.request.host = "appstore.mozillalabs.com"
    else:
      # defaults are fine
      self.request.host = "your_host:8400"
      pass
    
    return_to = self.get_argument("return_to", None)
    callback_uri = None
    if return_to:
      scheme, netloc, path, query, fragment = urlparse.urlsplit(self.request.uri)

      if HACKING:
        schemeAndHost = "http://your_host:8400"
      else:
        schemeAndHost = "https://appstore.mozillalabs.com"
      
      callback_uri = "%s%s?%s" % ( schemeAndHost,
        path, urllib.urlencode({"to":return_to})
      )
      logging.error("Sending OpenID request with callback of %s" % callback_uri)
    self.authenticate_redirect(callback_uri=callback_uri)

class GoogleIdentityHandler(OpenIDLoginHandler, tornado.auth.GoogleMixin):
  @tornado.web.asynchronous
  def get(self):
    self.handle_get()

  def getIdentifier(self, user):
    return user["email"]

class YahooIdentityHandler(OpenIDLoginHandler, tornado.auth.OpenIdMixin):
  _OPENID_ENDPOINT = "https://open.login.yahooapis.com/openid/op/auth"

  @tornado.web.asynchronous
  def get(self):
    self.handle_get()

  def getIdentifier(self, user):
    return user["email"]

class TwitterIdentityHandler(FederatedLoginHandler, tornado.auth.TwitterMixin):
  @tornado.web.asynchronous
  def get(self):
    if self.get_argument("oauth_token", None):
      self.get_authenticated_user(self.async_callback(self._on_auth))
      return
    self.authorize_redirect()

  def getIdentifier(self, user):
    return "%s@twitter.com" % user["username"] 


class XRDSHandler(WebHandler):
  def get(self):
    self.set_header("Content-Type", "application/xrds+xml")
    self.write("""<?xml version="1.0" encoding="UTF-8"?>"""\
      """<xrds:XRDS xmlns:xrds="xri://$xrds" xmlns:openid="http://openid.net/xmlns/1.0" xmlns="xri://$xrd*($v*2.0)">"""\
      """<XRD>"""\
      """<Service priority="1">"""\
      """<Type>https://specs.openid.net/auth/2.0/return_to</Type>"""\
      """<URI>https://appstore.mozillalabs.com/app/</URI>"""\
      """</Service>"""\
      """</XRD>"""\
      """</xrds:XRDS>""")





TaskTrackerApp = {"name":"Task Tracker", 
                "app":{
                  "urls":["http://tasktracker.mozillalabs.com/",
                          "https://tasktracker.mozillalabs.com/"],
                  "launch": {
                    "web_url":"https://tasktracker.mozillalabs.com/"
                  }
                },
                "description":"Manage tasks with ease using this flexible task management application.\n\nCreate To Do lists or manage complex hierarchical tasks chains.\n\nIntegrates with many web-based calendars and notification systems.",
                "icons":{
                  "96":"data:image/png;base64,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"
                }
                }

MozillaBallApp = {"name":"MozillaBall",
                "app":{
                  "urls":["http://mozillaball.mozillalabs.com/",
                          "https://mozillaball.mozillalabs.com/"],
                  "launch": {
                    "web_url":"https://mozillaball.mozillalabs.com/"
                  }
                },
                "description":"Fast and furious Open Web development game play!\n\nScore points by implementing Open Web features.\n\nWin powerups by upgrading your JavaScript engine, adding security features, and hardware acceleration.",
                "icons":{
                  "96":"data:image/png;base64,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"
                }
                }

GmailApp = {"name":"Gmail",
                "app":{
                  "urls":["http://mail.google.com/mail/",
                          "https://mail.google.com/mail/",
                          "http://www.google.com/mail/"
                          "https://www.google.com/mail/"],
                  "launch": {
                    "web_url":"http://mail.google.com/mail/"
                  }
                },
                "description":"7+ GB of storage, less spam, and mobile access. Gmail is email that's intuitive, efficient, and useful. And maybe even fun.",
                "icons":{
                  "96":"data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAYAAADimHc4AAAACXBIWXMAAAsTAAALEwEAmpwYAAARcklEQVR4Ae1cfWxcxREf52xMYjt2HMuOAzR3sgJOY7CDL5ivShYiCIRRGqVVUFrAatSWCFBKqlaVKIKQVuKfRq0ARW1FBQFSUhVRKqKWpkJGUBEHh3zYkOAQ7BBI4jQfduzYOP7q/PZunvee3/f5ckF9I93tvvd2Z/fNb2d2ZnfvciYmJiik7ElgRvaaDluGBEIAsjwOQgBCALIsgSw3H2pACECWJZDl5kMNCAHIsgSy3HyoASEAWZZAlpsPNSAEIMsSyHLzoQaEAGRZAlluPtSAEIAsSyDLzYcakGUAcvX2h499MtFxfEC/5ZqvqSykvHkL6auWrdTxbifVfOtqilQvVfdcK3OBkROHVDG/7XrhfSmWqa+vz9H7lSNbkrt371Z7kxCoH4LwQa1rV1P7qTMqf21ZqcojLS2fR325s9X9qqpKyqupp8Lr4uraz5cA5afOpVhWBpoAoQAIKny8IACAcF585BHq7e+j6KldFM2bfPWW4huMi5KiYiOPjACEPEDyqz2o93UERgfBMEEy8mVE4+W80vDJPlW0sW8XI5JaS92TW4lickX0BdEbGkB79+/kZ1vU84G82XRr5VwqLpyprgFQ9fImyi8vTjFvQfo72YGLn8OAgawFBAMAdCXoy+Se6Qz8JgBoYIyorfQGqvtGjMYG++j9vlEqHDlHez8/p7QKzKE9CYASTUF7IrOKFUC69uBp0PdIcM7sN/qma20KAEGbHjrWbwgqCI/CCBGAGBuM0fVPbKDrk0ygWSMdu9XV4cPHiXp7DIAw3/T2dxnNWWkPHkKDdO3BvUsJoGkB4NQnn+C90qajH2wj2kAKBDCDucm/7TbFtzaRqDwAAjjQPIAPcIpHz9GZkycstQeVABDmKGgSzNtNxbmGBpVdcw0VJttB2YsJ0LQA0DcwhH6nTWry3ruNujfPo+jatY78AA6VL6XCaiIBJ8o1RHvGDn6gwAET0Z73jp/mWfucMm/vdvVRd/84RYs4FGp9n6eeF1BUAQTz1hlJOAyLKiqMuQfPpxucaQEAdnu6CPNBCwtkOTN0A8GpTcQiAAckANUmLqdoD0xby0d7qeLLXXT2K6K3RhIFT0cKqI2zY//8h7oBsESD5pbOpUX5RFX1dcq1NjsHyaZck7QBwISCSXO6SOaDN1p5XmDPp/aH30uLtXgbqUx40ileRDXVhVQz/wPat61Tuc/Q4zmXayVHzquL4c8BA9EJHhwnOD0/ri6praCA6G+vqIuxeYtU2nznXRR//MlEAQ/faQOANuCx1MEFnUaKn9lFLf8hFRtc3rg6xXPw04y412YgcB9m6uXntlD+wR2WLEvYpe5NagMK5DNuekoXztMwgwLK62pTwLx3YAEhzBRPx81kpQ0AXsIrISiDYDHK3QhlUPbZp5+hh7kwQDCTvKT5vtW1ACHP9v3xFQb4LWV25F6QVEARIGCaQK/u2KnSe5c5zxs8A6VHXlxQZddZ+MsbblL+vtcWAQIia4zSgf0JM6DXxeiy++jlzPnuzZsN4c9kO25H+ui3K6Pfv4xNEuYFUFdXt0rdvtIGQHkYbq3wc0xeCzdsUiB4KG4UgWdU0rWDXt34S0OtjYcOGStgUPzDDU/Qvu2/UyPfSviYhP2SjH7Uw6QM6u096wmEtAGAB+GF4HuDAELxHeu8VDHKCAhYb/JjdgwGnMFqLeqrWIOvRfhDw3op/3kIX8wQJmIsNkofY7GoK8O0AYALKssJdq2dYq8BgY8QQBirW0Xd2gQnz+xSAeG59Y8ZL2hX1ny/beOTai6BJmWCdA2AO2qe8J3aTBsAJ+b6MyxL64Qlh6uW+gfhys7XlBnReTnl2366Vtl7zCVlLm9rZX7OXSAauKxAfZzawTPECTB9HR0dRlFcO5FLl5yqJlwt2QNwLkmEcF8ndAwg9MaW+dYEmBEI1olgBrBH0f3OH9REbiV8J/ODCRjC/yoWp9V3NBHMiz7S0bZ+jdggt/Qq1SWZgGtqapy6qJ6lBYDO3cm17C6b3BPQ6wCEex//FeG5X3MEwdqBoNt7CF6ED1PohUT48667gTY+9AOqXdVEkRMHDFtvxaOA2xEX1Oq53b1pAQBuphthcrIi7I6tub9ZgeCFj/CAULF8cHbr7+WWSnV7L4KXAvq13eiH8LEE8d3lq2jdC1tozuofq61W4WGXjszkqFijkpI52pV9dnJmtC9j+wRBGFYYQRCelRbgPlxQp21IvOQa5vH8lsSytGLo8iWBGuo8PL9IBWrK3jMoVvbebfTD/kP4J6+Ik3k5ASuteUO8LOESQGLhDgQX9KIAgCDMjdxeXOoDhOW8rL1ve+qWpjy3SvVA7dptbyoXMw6dxscHQfiHxwsodvOdtP4XP58yWLDMbSax/3BBkcccgS1VcUFR3hx9m3ngOi0NwD6AckGTnO20AMu7XgjuKV62mydZtTTtoRLMStmRHXSU92Z0E+NUVTc/EH5neZzW3X6LilGs6jk5GgIE6o2W8omQJINYLJrMOSdpAWC1DyB2XDdH2Dr0SvCMsCnjBQRpC7zthG/WQLPwZ9Uto40rVihbb9dHMbN2z3EfLqjfGAD1fCorqkyS0z6ALhzZWJ+saZ/z4p6Ct87fnpv9k9bZcWq4fRU98MwzjsKHScHGjU76qJf7cEHRdz8xAOoGBgAdg2o6CUKemWMA6bRdihf5/kP3W7qnwtOurn7favTD5PRckXAvGzZvdd3h8rLaixhAIn0/MQD6mpYJ0l/WLg8ffyZ7KX4Jy89reJKHl0Meolidv1nweCamZ3zpSnrQYqLV6+t5OBp6DGA1+hEDYAuzQa/oMR8YAGyKu9lGEQS2B4OQeEbYHSvjvYGgBOFj1DcurqPan613HfV6O15WexED3Dh3pqrmxwVFheAmKHlcBExE0MibCTEATEpQkiVsnBtyagf88VwvA8HjM1y9TAV78d9s9t8XXu1VMQDztxr9aBcuqNnMenFBUTewBsAFRcBzClyY8OJ2nkiiRPBvbM438smL7nestUAXurQCwWOive+6KrXmFHQQ4CQFbLwsOQt/c+p3GVrqBwbAygUVQehAeI0BpENWKcwdNjrGBlepYEvnL22a66mJ9v5mdd4nqPDBE/vdILvRrx7yVxAXFHUDAyAuKEaabG5IZ3RtMC9DSxkvKTytgbffVpscmA8W83qSOoHBS9JWJH3BMncTL6BZ7SNb1bO7h/Yxz2GS1UmiX7lXN3+OMm0dO16SW55NnYm1Ud814xQdorKMTBwL9Et48T179qiNDZxYk3UkpA/+6NtqCVvnCcHjg1Ffe/c6Wrx2vTqUBT7pEDTPHAOY+cE8yW6fXxcUvAJpgLwYXhokqa4JuAcbvJF/E+CHsPl+aCyi1lGsTAdG9Up2DV97ndU+eZwE7WJfYQ1HtAAM9fAzE5x8wLkiKz5e+oRzqXBBzWQ2R0GWoYVnIABQGaqZWPsTVlOBQHjuNQYAqNjKW8iLKUuWLJlkapGDkOv4PGgL9wEjtPnG66n6gTWGpqAKtKWWtwfTAcGLC4qTEOlQIAAkOkRUmXKSLNkT0QhcusUAEDz4HeCTam6ClxfFiNZPzNmNcpTDbwoCg8Au6IXz5ynPxlDD/ETYBW26efIkhNdlaHmXQADgt2BHD31EVdwxOxDQgFsMYJgbDtSW+IwVdBCcTAw0oZr7EgQEdZhXJOWQ6i4oinmNAVA2EADmoyhWIODeePIoChrSyRB8ZbFvwet8nASvl/MDgsxv4C0uqM5Lz8M74tdMoVgsmnLtdmGjXM7VDiQnX+wgCUHg+OiEnxjJC+E+8vBuMMnC3HgVoM4zaF6BkDRHep90fvp95GWpxSkIgwsaNAZA24E04PQZPmefJICAQ6xCAgJ2mO7iX6dAyHgZuHQieCl7sVOAEGHwzeZIF7z0SVxQcwwgz5GKC4p3DBIDgEcgDYBqFvLJYCFdE+QeUsQAMDfwbjBKvE6yOo/pzqMPMjFbCV7a011Qs9spZZCKCxokBkB93xqgqyY6JuppBmHsikXgr+hSELz0BalZE/Rnkr8YLija8q0BoprSUYBgHiFyjZGGl70UCYMi0rRcmSOr/uEkBFxQO4L5AQU5CZGomfj2DYCumjojMxAIwi5V4Uu/rUCQc507Tw9JMdtUTkLoBfy4oKjn2wS5qaYAgRjAycZeTA9IF5A5DxD28E1MzNBYOfyjOxrmOvo1TkI4vade1irvG4ADPT1WfKbckwUqjCirURGk05kCTUB4fV8HrajlM6DssbnFAPoLi9bEYlH9tqe8bwAwMtxOicE+IgbAi2BESQftegSA7IDS67iBlg5AAGFhZIwOvvGmOmC199hZvWnLPGIAmNmOl/wvQwtD3wBgZMgEJB6QMJMUC1R+jqIIQJIKH3NqpUl6GTeApKwdUBAmli2OvrZVijqmouVBXVAw9w1AfkWMIlVxFYKPaEu14jFI4LL90Bd064vPc5RWQfibGpD5gC4CMz/kBhB4uYGEMk5AAYQ+1gIsQ19u46JgAKa7Cop+gHwDcN+zm+lejmxBMDHwig7v3ktYnoB5goZAfT89ckR9VMHkV7ToBeMMPQIYuHACDpatMaEJ+QVH6nkBCWXtgHICR9qQVIIwuQ6S+gYAjYgK583jCx4xcf4Fqe7ty0tgmRkrp5i4xasYPXNUreEDID5tkkKwqaLWmEN07ZkugKRBO6AUMA7L0GJ+4YKmGwOgL4EAQEUsqoFwFM/8SxC8BEDCJ96YCg7qACDRHpyuEJ8bIAEYkKTqgr90cER78Ew0SDdvQbUH/NAvu2VoET7KgaTtxJW9Vslzq1QBwH9b1sIPGyEYGd1WhfV78BpQvqbyxileTuJHyjuN4jpAk+DwY9Ye/IxBPzVkpT1gZDZvuvYg6Cspet3QHjNAuvZ4AcfKBTULH30CSX9jsShfuZOUT8o8oQF88Wj7sf7Hrp1f9B0p4M5qssRUezr1t1FyaDXx+7VJcCa58AiqqTFsM/Z+7bQHdXBaAkGhxCUASMwb0tY9ybUCLutVewAO/gJHlqGlb1bCB09onW7KkK8h94MALOu/ssx/Df7Gn/a1trZW5ObmPlRTWXA3TeQsphxK/uZbupF+qndW5ybg6PfMedEiAdtOUzGAZO7BxpHsXQz3dBmC1U86JLQn8R9CaBPa07rnQxo73GbuQsr1NbetpJ9s/Qu9xDEAzDEsgj6AUgrjYoKGWdofdRw/v310dPS5hoYGFdEaAEgFBuJKBuKbfF02Pj6eP2PGDH++ojAKmO7fv1/VzGHiUcLNz8j97LPPCvmyfGIiZwHfrUqkynpRLBZV5fWXtwMHBQGQaI8AJOZNMeIvCcL0ExHiZksZAeCppzYaP0m6554m4j7/m8vs5/Q4932A0wvcd2xVnWLBf8yC/0J4IJ0yCScLpBTSK2Q6z3/n6NhEf+fustxZMwFC5YGTg8VceBZ/ZvOLlv/5X+8v4HxVTs5OpJheUhwE0R4c8hKvTVKUBWH/oo7jAFDx6C3KQdABgnnSNUgV5C8W/n9ZCzYNDg7+qbCw8KTcd0unAOBWIdvPi66ux3FUfKj+qtTexONxsgOovb29vL2dbAEScBCIxfkjZHYQRHvEA0qchi7BhLOJR/zTLHyp6imdYoI81foaFxKAPu4ZQnhuaBAA4msFEKdRNhsleE2nuQe2H3NAXV3db5ubmx9Feb/0fweAm4AEoPZjA/N5ROPXhaUMTgUDEuU8m7cc6F0Rm7yhrq4jB2OxBX+PRqObGhsbTUcSuJQHCgHwICQp0v/lwbmR8ZzYxz39JeygDEcikU95zjouz4OkIQBBpDaNdWzW+6axhZCVowRCABzFk/mHIQCZl7FjCyEAjuLJ/MMQgMzL2LGFEABH8WT+YQhA5mXs2EIIgKN4Mv8wBCDzMnZsIQTAUTyZfxgCkHkZO7YQAuAonsw/DAHIvIwdWwgBcBRP5h+GAGRexo4thAA4iifzD/8HhLKipr7N1VgAAAAASUVORK5CYII="
                }
                }

      
def create_bogus_apps():
  try:
    model.application(1)
  except:
    app = model.createApplication(json.dumps(TaskTrackerApp), json.dumps(TaskTrackerApp))
    app.price = 199
    model.save(app)
  
  try:
    model.application(2)
  except:
    model.createApplication(json.dumps(MozillaBallApp), json.dumps(MozillaBallApp))

  try:
    model.application(3)
  except:
    model.createApplication(json.dumps(GmailApp), json.dumps(GmailApp))
  
##################################################################
# Main Application Setup
##################################################################

settings = {
    "static_path": os.path.join(os.path.dirname(__file__), "static"),
    "cookie_secret": config.cookie_secret,
    "login_url": "/login",
    "debug":True,
    "xheaders":True,

    "twitter_consumer_key":"HvhrjQU3EKYZttdBglHT4Q",
    "twitter_consumer_secret":"ajyQvZn3hDLcVI9VYZfwZi3kxsF8g8arayxzoyPBIo",
#    "xsrf_cookies": True,
}

application = tornado.web.Application([
    (r"/app/(.*)", AppHandler),
    (r"/xrds", XRDSHandler),
    (r"/login", LoginHandler),
    (r"/logout", LogoutHandler),
    (r"/verify/(.*)", VerifyHandler),
    (r"/api/buy", BuyHandler),
    (r"/unregister/(.*)", UnregisterHandler),
    (r"/account", AccountHandler),
    (r"/account/addid/google", GoogleIdentityHandler),    
    (r"/account/addid/yahoo", YahooIdentityHandler),    
    (r"/account/addid/twitter", TwitterIdentityHandler),    
    (r"/", MainHandler),
 
	], **settings)


def run():
    http_server = tornado.httpserver.HTTPServer(application)
    http_server.listen(8400)
    
    create_bogus_apps()
    
    tornado.ioloop.IOLoop.instance().start()
		
import logging
import sys
if __name__ == '__main__':
	if '-test' in sys.argv:
		import doctest
		doctest.testmod()
	else:
		logging.basicConfig(level = logging.DEBUG)
		run()
	
	