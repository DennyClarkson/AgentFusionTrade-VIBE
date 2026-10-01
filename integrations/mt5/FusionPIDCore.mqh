// Root-authored, deterministic PID centre. All distances are quote-price units.
#ifndef FUSION_PID_CORE
#define FUSION_PID_CORE
struct PIDSettings
{
   double kp,ki,kd,filter_seconds,max_speed,integral_limit,max_dt;
};
double PIDClamp(const double x,const double lo,const double hi) {return MathMax(lo,MathMin(hi,x));}
class FusionPID
{
public:
   double centre,integral,derivative,previous_error;
   void Reset(const double value) {centre=value;integral=0;derivative=0;previous_error=0;}
   double Move(const double quote,double dt,const PIDSettings &p)
   {
      if(dt<=0) return centre;
      double error=quote-centre;
      if(dt>p.max_dt) {integral=0;derivative=0;previous_error=error;}
      dt=MathMin(dt,p.max_dt);
      derivative+=dt/(p.filter_seconds+dt)*((error-previous_error)/dt-derivative);
      double candidate=p.ki>0 ? PIDClamp(integral+error*dt,-p.integral_limit,p.integral_limit) : 0;
      double raw=p.kp*error+p.ki*candidate+p.kd*derivative;
      double move=PIDClamp(PIDClamp(raw,-p.max_speed,p.max_speed)*dt,MathMin(0,error),MathMax(0,error));
      if((raw-move/dt)*error>1e-12)
      {
         raw=p.kp*error+p.ki*integral+p.kd*derivative;
         move=PIDClamp(PIDClamp(raw,-p.max_speed,p.max_speed)*dt,MathMin(0,error),MathMax(0,error));
      }
      else integral=candidate;
      centre+=move;previous_error=error;return centre;
   }
};
// Call BEFORE Move. Both directions are exits, independent of position side.
int PIDCrossing(const double quote,const double centre,const double lower,const double upper)
{
   if(quote<=centre-lower) return -1;
   if(quote>=centre+upper) return 1;
   return 0;
}
bool PIDControllerSelfTest()
{
   PIDSettings p;p.kp=.8;p.ki=.04;p.kd=.12;p.filter_seconds=.35;p.max_speed=1.5;p.integral_limit=20;p.max_dt=.25;
   FusionPID c;c.Reset(2500);
   double q[]={2500.2,2500.4,2499.8,2501,2501};
   double dt[]={.1,.15,.08,0,2};
   // Independently executed Python reference values, including duplicate time and reset gap.
   double expected[]={2500.0214133333334,2500.0793335813332,2500.0521639358485,2500.0521639358485,2500.2441007388393};
   for(int i=0;i<5;i++) if(MathAbs(c.Move(q[i],dt[i],p)-expected[i])>1e-8) return false;
   if(PIDCrossing(2501,2500,1,1)!=1 || PIDCrossing(2499,2500,1,1)!=-1 || PIDCrossing(2500,2500,1,1)!=0) return false;
   c.Reset(2500);p.kp=100;p.ki=100;p.kd=100;p.max_speed=.1;
   for(int i=0;i<100;i++) {double old=c.centre;c.Move(2510,.1,p);if(c.centre<old || c.centre>2510 || MathAbs(c.integral)>1e-9) return false;}
   return true;
}
#endif
