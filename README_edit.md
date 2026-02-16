Tryout steps 

1. create bmh objects and secrets:

kez@kez-dev-vm-1:~/Documents/python_projects/daalu_full_python/daalu_io/daalu/assets/bmh$ k apply -f cp02.yaml
baremetalhost.metal3.io/cp02 created
secret/bmh-cred-cp02 created
kez@kez-dev-vm-1:~/Documents/python_projects/daalu_full_python/daalu_io/daalu/assets/bmh$ k apply -f cp01.yaml
baremetalhost.metal3.io/cp01 created
secret/bmh-cred-cp01 created

1a. verify bmh objects 
k get bmh -A
k describe bmh cp01 -n baremetal-operator-system 


