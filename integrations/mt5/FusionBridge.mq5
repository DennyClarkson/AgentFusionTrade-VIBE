#property copyright "AgentTradeFusion"
#property version   "2.00"
#property description "M1 strategy signal and parameter companion. Execution via Python gateway."
#property strict

input string MonitorSymbol = "XAUUSD";
input int PollSeconds = 5;
input bool ExportCalendar = true;
input ENUM_TIMEFRAMES AnalysisTimeframe = PERIOD_M5;
input int FastEMA = 20;
input int SlowEMA = 50;
input int ATRPeriod = 14;
input double StopATR = 1.5;
input double RewardRisk = 1.5;

string active_symbol;
int active_interval;
int fast_handle=INVALID_HANDLE,slow_handle=INVALID_HANDLE,atr_handle=INVALID_HANDLE;
double active_stop,active_reward;
long config_revision = 0;
datetime calendar_updated = 0;
string folder = "AgentTradeFusion\\";

bool SetIndicators(const string symbol,const ENUM_TIMEFRAMES tf,const int fast,const int slow,const int atr)
{
   int f=iMA(symbol,tf,fast,0,MODE_EMA,PRICE_CLOSE);
   int s=iMA(symbol,tf,slow,0,MODE_EMA,PRICE_CLOSE);
   int a=iATR(symbol,tf,atr);
   if(f==INVALID_HANDLE || s==INVALID_HANDLE || a==INVALID_HANDLE)
   {
      if(f!=INVALID_HANDLE) IndicatorRelease(f);
      if(s!=INVALID_HANDLE) IndicatorRelease(s);
      if(a!=INVALID_HANDLE) IndicatorRelease(a);
      return false;
   }
   // MT5 can return the SAME cached handle for identical parameters.
   // Releasing that old handle also invalidates the newly assigned one.
   if(fast_handle!=INVALID_HANDLE && fast_handle!=f) IndicatorRelease(fast_handle);
   if(slow_handle!=INVALID_HANDLE && slow_handle!=s) IndicatorRelease(slow_handle);
   if(atr_handle!=INVALID_HANDLE && atr_handle!=a) IndicatorRelease(atr_handle);
   fast_handle=f;slow_handle=s;atr_handle=a;
   return true;
}

ENUM_TIMEFRAMES ParseTimeframe(const string name)
{
   if(name=="M1") return PERIOD_M1;
   if(name=="M5") return PERIOD_M5;
   if(name=="M15") return PERIOD_M15;
   if(name=="M30") return PERIOD_M30;
   if(name=="H1") return PERIOD_H1;
   if(name=="H4") return PERIOD_H4;
   if(name=="D1") return PERIOD_D1;
   return PERIOD_CURRENT;
}

bool Publish(const string temporary, const string target)
{
   return FileMove(folder+temporary, FILE_COMMON, folder+target, FILE_COMMON|FILE_REWRITE);
}

void ReadParameters()
{
   int f=FileOpen(folder+"parameters.csv",FILE_READ|FILE_CSV|FILE_ANSI|FILE_COMMON,';',CP_UTF8);
   if(f==INVALID_HANDLE) return;
   string protocol=FileReadString(f);
   long revision=(long)StringToInteger(FileReadString(f));
   string symbol=FileReadString(f);
   int interval=(int)StringToInteger(FileReadString(f));
   ENUM_TIMEFRAMES tf=ParseTimeframe(FileReadString(f));
   int fast=(int)StringToInteger(FileReadString(f));
   int slow=(int)StringToInteger(FileReadString(f));
   int atr=(int)StringToInteger(FileReadString(f));
   double stop=StringToDouble(FileReadString(f));
   double reward=StringToDouble(FileReadString(f));
   FileClose(f);
   if(protocol!="FUSION2" || revision<=config_revision || interval<2 || interval>300 || StringLen(symbol)<1 || StringLen(symbol)>40) return;
   if(tf==PERIOD_CURRENT || fast<2 || fast>200 || slow<=fast || slow>400 || atr<3 || atr>100 || !MathIsValidNumber(stop) || stop<0.5 || stop>10 || !MathIsValidNumber(reward) || reward<1 || reward>10) return;
   if(!SymbolSelect(symbol,true)) return;
   if(!SetIndicators(symbol,tf,fast,slow,atr)) return;
   active_symbol=symbol;
   active_interval=interval;
   active_stop=stop;active_reward=reward;
   config_revision=revision;
   EventKillTimer();
   EventSetTimer(active_interval);
}

void WriteTelemetry()
{
   MqlTick tick;
   if(!SymbolInfoTick(active_symbol,tick)) return;
   double fast[],slow[],atr[];
   if(CopyBuffer(fast_handle,0,1,1,fast)!=1 || CopyBuffer(slow_handle,0,1,1,slow)!=1 || CopyBuffer(atr_handle,0,1,1,atr)!=1) return;
   int f=FileOpen(folder+"telemetry.tmp",FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON,';',CP_UTF8);
   if(f==INVALID_HANDLE) return;
   FileWrite(f,"protocol","revision","symbol","raw_tick_time","bid","ask","account_mode","calendar_updated","login","server","ema_fast","ema_slow","atr","stop_atr","reward_risk","strategy_revision","signal","signal_reason","heartbeat_utc","ea_version");
   FileWrite(f,"FUSION3",config_revision,active_symbol,(long)tick.time,DoubleToString(tick.bid,8),DoubleToString(tick.ask,8),(int)AccountInfoInteger(ACCOUNT_TRADE_MODE),(long)calendar_updated,AccountInfoInteger(ACCOUNT_LOGIN),AccountInfoString(ACCOUNT_SERVER),fast[0],slow[0],atr[0],active_stop,active_reward,strategy_revision,ea_signal,ea_reason,(long)TimeGMT(),"2.00");
   FileClose(f);
   Publish("telemetry.tmp","telemetry.csv");
}

void WriteCalendar()
{
   if(!ExportCalendar || TimeTradeServer()-calendar_updated<60) return;
   MqlCalendarValue values[];
   datetime now=TimeTradeServer();
   int count=CalendarValueHistory(values,now-3600,now+86400,NULL,"USD");
   if(count<0) { Print("Fusion calendar unavailable: ",GetLastError()); return; }
   int f=FileOpen(folder+"calendar.tmp",FILE_WRITE|FILE_CSV|FILE_ANSI|FILE_COMMON,';',CP_UTF8);
   if(f==INVALID_HANDLE) return;
   // Calendar timestamps are documented trade-server time, independently of tick raw times.
   long offset=(long)MathRound((double)(TimeTradeServer()-TimeGMT())/900.0)*900;
   FileWrite(f,"raw_time","time_utc","server_offset_seconds","title","impact","currency");
   for(int i=0;i<count && i<500;i++)
   {
      MqlCalendarEvent event;
      if(!CalendarEventById(values[i].event_id,event)) continue;
      string importance=event.importance==CALENDAR_IMPORTANCE_HIGH ? "high" : event.importance==CALENDAR_IMPORTANCE_MODERATE ? "medium" : "low";
      FileWrite(f,(long)values[i].time,(long)values[i].time-offset,offset,event.name,importance,"USD");
   }
   FileClose(f);
   if(Publish("calendar.tmp","calendar.csv")) calendar_updated=now;
}

int OnInit()
{
   if(PollSeconds<2 || PollSeconds>300 || FastEMA<2 || SlowEMA<=FastEMA || ATRPeriod<3) return INIT_PARAMETERS_INCORRECT;
   active_symbol=MonitorSymbol;
   active_interval=PollSeconds;
   active_stop=StopATR;active_reward=RewardRisk;
   FolderCreate("AgentTradeFusion",FILE_COMMON);
   if(!SymbolSelect(active_symbol,true)) return INIT_FAILED;
   if(!SetIndicators(active_symbol,AnalysisTimeframe,FastEMA,SlowEMA,ATRPeriod)) return INIT_FAILED;
   EventSetTimer(active_interval);
   Print("FusionBridge: read-only mode, no order execution.");
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   ReadParameters();
   ReadStrategy();
   ComputeStrategySignal();
   WriteTelemetry();
   WriteCalendar();
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(fast_handle!=INVALID_HANDLE) IndicatorRelease(fast_handle);
   if(slow_handle!=INVALID_HANDLE) IndicatorRelease(slow_handle);
   if(atr_handle!=INVALID_HANDLE) IndicatorRelease(atr_handle);
}
// Fixed, root-authored M1 signal kernel. Orders remain at the serialized Python gateway.
struct FusionFrame
{
   double fast,slow,atr,adx,slope,lower,upper,previous_lower,previous_upper,open,close,low,high;
   datetime bar_time;
};
long strategy_revision=0;
int ea_fast=20,ea_slow=50,ea_atr=14,ea_bb=20,ea_hold=45;
double ea_deviation=2,ea_separation=.2,ea_adx=20,ea_slope=.05,ea_stop=1.5,ea_reward=2,ea_trail_start=1,ea_trail=1,ea_breakeven=.8;
string ea_bias="H1",ea_signal="HOLD",ea_reason="parameters not acknowledged";

void ReadStrategy()
{
   int f=FileOpen(folder+"strategy.csv",FILE_READ|FILE_CSV|FILE_ANSI|FILE_COMMON|FILE_SHARE_READ,';',CP_UTF8);
   if(f==INVALID_HANDLE) return;
   string protocol=FileReadString(f);
   long revision=(long)StringToInteger(FileReadString(f));
   long login=(long)StringToInteger(FileReadString(f));
   string server=FileReadString(f),symbol=FileReadString(f);
   int fast=(int)StringToInteger(FileReadString(f)),slow=(int)StringToInteger(FileReadString(f));
   int atr=(int)StringToInteger(FileReadString(f)),bb=(int)StringToInteger(FileReadString(f));
   double deviation=StringToDouble(FileReadString(f)),separation=StringToDouble(FileReadString(f));
   double adx=StringToDouble(FileReadString(f)),slope=StringToDouble(FileReadString(f));
   string bias=FileReadString(f);
   double stop=StringToDouble(FileReadString(f)),reward=StringToDouble(FileReadString(f));
   double start=StringToDouble(FileReadString(f)),trail=StringToDouble(FileReadString(f)),breakeven=StringToDouble(FileReadString(f));
   int hold=(int)StringToInteger(FileReadString(f));
   FileClose(f);
   if(protocol!="FUSION_EA1" || revision<=strategy_revision || login!=AccountInfoInteger(ACCOUNT_LOGIN) || server!=AccountInfoString(ACCOUNT_SERVER) || symbol!=active_symbol) return;
   if(fast<2 || fast>200 || slow<=fast || slow>400 || atr<3 || atr>100 || bb<10 || bb>60) return;
   if(!MathIsValidNumber(deviation) || deviation<1 || deviation>3.5 || !MathIsValidNumber(separation) || separation<0 || separation>5) return;
   if(!MathIsValidNumber(adx) || adx<10 || adx>40 || !MathIsValidNumber(slope) || slope<0 || slope>.5) return;
   if(bias!="H1" && bias!="D1" && bias!="consensus") return;
   if(!MathIsValidNumber(stop) || stop<.5 || stop>10 || !MathIsValidNumber(reward) || reward<1 || reward>10) return;
   if(!MathIsValidNumber(start) || start<.5 || start>3 || !MathIsValidNumber(trail) || trail<.3 || trail>3 || !MathIsValidNumber(breakeven) || breakeven<.3 || breakeven>3 || hold<5 || hold>1440) return;
   ea_fast=fast;ea_slow=slow;ea_atr=atr;ea_bb=bb;ea_deviation=deviation;ea_separation=separation;ea_adx=adx;ea_slope=slope;
   ea_bias=bias;ea_stop=stop;ea_reward=reward;ea_trail_start=start;ea_trail=trail;ea_breakeven=breakeven;ea_hold=hold;
   strategy_revision=revision;
   Print("Fusion strategy parameters acknowledged: ",strategy_revision);
}

bool ComputeFrame(const ENUM_TIMEFRAMES timeframe,FusionFrame &frame)
{
   MqlRates bars[];
   ArraySetAsSeries(bars,false);
   int needed=MathMax(600,ea_slow*3);
   int count=CopyRates(active_symbol,timeframe,1,needed,bars);
   if(count<ea_slow*3 || count<ea_bb+5) return false;
   double fast=bars[0].close,slow=bars[0].close,atr=0,adx=0,adtr=0,plus=0,minus=0;
   double ema_history[];ArrayResize(ema_history,count);
   double previous_lower=0,previous_upper=0,lower=0,upper=0;
   for(int i=0;i<count;i++)
   {
      fast+=2.0/(ea_fast+1)*(bars[i].close-fast);
      slow+=2.0/(ea_slow+1)*(bars[i].close-slow);
      ema_history[i]=fast;
      int p=MathMax(0,i-1);
      double tr=MathMax(bars[i].high-bars[i].low,MathMax(MathAbs(bars[i].high-bars[p].close),MathAbs(bars[i].low-bars[p].close)));
      atr+=(tr-atr)/MathMin(i+1,ea_atr);
      int dn=MathMin(i+1,14);
      double up=bars[i].high-bars[p].high,down=bars[p].low-bars[i].low;
      adtr+=(tr-adtr)/dn;
      plus+=((up>down && up>0 ? up : 0)-plus)/dn;
      minus+=((down>up && down>0 ? down : 0)-minus)/dn;
      double di_plus=adtr>0 ? 100*plus/adtr : 0,di_minus=adtr>0 ? 100*minus/adtr : 0;
      double dx=di_plus+di_minus>0 ? 100*MathAbs(di_plus-di_minus)/(di_plus+di_minus) : 0;
      adx+=(dx-adx)/dn;
      previous_lower=lower;previous_upper=upper;
      int from=MathMax(0,i-ea_bb+1),n=i-from+1;
      double mean=0,variance=0;
      for(int j=from;j<=i;j++) mean+=bars[j].close;
      mean/=n;
      for(int j=from;j<=i;j++) variance+=MathPow(bars[j].close-mean,2);
      double sd=MathSqrt(variance/n);
      lower=mean-ea_deviation*sd;upper=mean+ea_deviation*sd;
   }
   frame.fast=fast;frame.slow=slow;frame.atr=atr;frame.adx=adx;
   frame.slope=atr>0 ? (fast-ema_history[count-6])/atr/5 : 0;
   frame.lower=lower;frame.upper=upper;frame.previous_lower=previous_lower;frame.previous_upper=previous_upper;
   frame.open=bars[count-1].open;frame.close=bars[count-1].close;frame.low=bars[count-1].low;frame.high=bars[count-1].high;frame.bar_time=bars[count-1].time;
   return true;
}

int FrameDirection(const FusionFrame &frame,const bool strict)
{
   if(frame.atr<=0 || MathAbs(frame.fast-frame.slow)/frame.atr<ea_separation) return 0;
   int direction=frame.fast>frame.slow ? 1 : -1;
   if(strict && frame.slope*direction<ea_slope && frame.adx<ea_adx) return 0;
   return direction;
}

void ComputeStrategySignal()
{
   ea_signal="HOLD";
   if(strategy_revision==0) {ea_reason="awaiting strategy parameters";return;}
   FusionFrame minute,hour,day;
   if(!ComputeFrame(PERIOD_M1,minute) || !ComputeFrame(PERIOD_H1,hour) || !ComputeFrame(PERIOD_D1,day)) {ea_reason="history warming";return;}
   int direction=FrameDirection(minute,true);
   if(direction==0)
   {
      int h=FrameDirection(hour,false),d=FrameDirection(day,false);
      direction=ea_bias=="H1" ? h : ea_bias=="D1" ? d : h==d ? h : 0;
   }
   if(direction==1 && minute.low<=minute.previous_lower && minute.close>minute.lower && minute.close>minute.open) ea_signal="BUY";
   if(direction==-1 && minute.high>=minute.previous_upper && minute.close<minute.upper && minute.close<minute.open) ea_signal="SELL";
   ea_reason=direction==1 ? "long direction; closed lower-band recovery required" : direction==-1 ? "short direction; closed upper-band recovery required" : "uncertain higher-timeframe bias";
}
